"""Third-pass critic that softens grounding-critic warnings when the linked
repo actually implements the behaviour the case tests.

The AC-grounding critic (:mod:`grounding_critic`) marks a case ungrounded
whenever the AC text doesn't spell out the behaviour under test. That's the
right call for hallucinations, but produces false positives whenever a case
tests a real, shipped implementation detail the AC never bothered to
enumerate — cache invalidation for a "cached" AC, streaming latency for a
"fast" AC, bitrate for a "quality" AC. QA saw those as "missing UI" when
the code was right there.

This module runs after ``grounding_critic`` has populated
``test_plan.grounding_warnings``. For each just-added ``critic_ac`` warning
it:

  1. Picks a linked repo from the ticket's PRs.
  2. Uses the GitHub code-search API to fetch up to a few files whose
     content matches the case's title.
  3. Asks the LLM whether those snippets implement the behaviour the case
     asserts.
  4. If yes, downgrades the warning to informational (``severity="info"``)
     with a short "beyond the AC but present in code" note, and clears the
     matching case's ``needs_manual_verification`` badge.

Failures — no github token, no linked repo, code-search miss, LLM error —
degrade gracefully: the warning stays at ``severity="warn"`` and QA sees
the same pre-recheck output.
"""

from __future__ import annotations

from typing import Iterable


_MAX_STEPS_IN_CRITIC_INPUT = 6
_MAX_STEP_LEN = 240
_MAX_SNIPPET_LEN = 3200
_MAX_FILES_PER_WARNING = 3
_MAX_WARNINGS_PER_PLAN = 8


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _iter_case_sections(test_plan) -> Iterable[tuple[str, int, dict]]:
    buckets = (
        ("happy_path", getattr(test_plan, "happy_path", None) or []),
        ("edge_cases", getattr(test_plan, "edge_cases", None) or []),
        ("integration_tests", getattr(test_plan, "integration_tests", None) or []),
    )
    for section, cases in buckets:
        for i, case in enumerate(cases):
            if isinstance(case, dict):
                yield section, i, case


def select_recheckable_warnings(
    warnings: list[dict],
    limit: int = _MAX_WARNINGS_PER_PLAN,
) -> list[dict]:
    """Return the subset of ``grounding_warnings`` this critic will re-check.

    Only warnings tagged ``source == "critic_ac"`` — the ones produced by
    the AC-grounding critic — are eligible. LLM-native warnings already
    reflect a diff/testID search; fix-scope warnings are about PR intent
    rather than code presence.

    Capped at ``limit`` to bound the GitHub-search + LLM cost per plan.
    """
    out: list[dict] = []
    for w in warnings or []:
        if not isinstance(w, dict):
            continue
        if w.get("source") != "critic_ac":
            continue
        # Skip anything already downgraded on a previous pass.
        if w.get("severity") == "info":
            continue
        if not (w.get("ac_id") and w.get("missing_element")):
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def extract_repos(dev_infos: list[dict]) -> list[str]:
    """Return the distinct ``owner/repo`` values across every PR in the
    ticket batch's development info.

    Order-preserving (first-seen wins) so tests can pin the primary repo.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in dev_infos or []:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("development_info") or {}
        for pr in dev.get("pull_requests") or []:
            if not isinstance(pr, dict):
                continue
            repo = (pr.get("repository") or "").strip()
            if not repo or repo in seen:
                continue
            seen.add(repo)
            out.append(repo)
    return out


def build_search_query(warning: dict) -> str:
    """Build a GitHub code-search query string from a warning's case title.

    The AC-critic's ``missing_element`` field holds the verbatim case title
    ("Empty audio synthesis does not overwrite existing cached audio"). We
    strip generic scaffolding words that don't help GitHub's ranker and
    keep the domain-specific nouns/verbs the code is likely to contain.
    """
    title = (warning.get("missing_element") or "").strip()
    if not title:
        return ""

    # Preserve token order — GitHub's search ranks by token relevance and
    # the first few words of a case title are usually the highest-signal.
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in title.split():
        # GitHub code search treats punctuation as separators; strip it
        # before deduping so "cache," and "cache" collapse.
        token = "".join(ch for ch in raw if ch.isalnum() or ch in "_-").lower()
        if not token or token in _QUERY_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 6:
            break
    return " ".join(tokens)


_QUERY_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "not", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "into", "as", "is", "are", "be", "been",
    "being", "was", "were", "does", "do", "did", "when", "if", "then",
    "backend", "frontend",
})


def _case_by_title(test_plan, title: str) -> tuple[str, int, dict] | None:
    """Locate the test case whose title matches ``title`` exactly.

    The critic pass stores the case title in ``missing_element``, so this
    is the reverse lookup used to clear ``needs_manual_verification`` and
    fetch the case body for the LLM prompt. Case-insensitive so trailing-
    whitespace or capitalization drift doesn't blow up the match.
    """
    normalized = (title or "").strip().lower()
    if not normalized:
        return None
    for section, idx, case in _iter_case_sections(test_plan):
        case_title = (case.get("title") or "").strip().lower()
        if case_title == normalized:
            return section, idx, case
    return None


def build_code_verification_inputs(
    test_plan,
    warnings: list[dict],
    hits_by_warning: dict[int, list[dict]],
) -> list[dict]:
    """Assemble the ``(warning, case body, code snippets)`` payload for the LLM.

    ``hits_by_warning`` is keyed by the warning's index in ``warnings`` —
    each value is a list of ``{"path", "ref", "content"}`` dicts as
    returned by :meth:`GitHubClient.search_relevant_files`.

    A warning is skipped when we have no code hits to show — asking the
    LLM to verify against zero evidence would just produce noise.
    """
    out: list[dict] = []
    for i, warning in enumerate(warnings):
        hits = hits_by_warning.get(i) or []
        if not hits:
            continue
        title = (warning.get("missing_element") or "").strip()
        located = _case_by_title(test_plan, title)
        case_body: dict = {}
        if located:
            _, _, case = located
            steps_raw = case.get("steps") or []
            steps: list[str] = []
            for step in steps_raw[:_MAX_STEPS_IN_CRITIC_INPUT]:
                if isinstance(step, str) and step.strip():
                    steps.append(_truncate(step.strip(), _MAX_STEP_LEN))
            case_body = {
                "steps": steps,
                "expected": _truncate((case.get("expected") or "").strip(), _MAX_STEP_LEN),
            }
        snippets: list[dict] = []
        for hit in hits[:_MAX_FILES_PER_WARNING]:
            if not isinstance(hit, dict):
                continue
            path = (hit.get("path") or "").strip()
            content = hit.get("content") or ""
            if not path or not content:
                continue
            snippets.append({
                "path": path,
                "ref": (hit.get("ref") or "").strip(),
                "content": _truncate(content, _MAX_SNIPPET_LEN),
            })
        if not snippets:
            continue
        out.append({
            "warning_key": _warning_key(warning),
            "ac_id": warning["ac_id"],
            "title": title,
            "steps": case_body.get("steps") or [],
            "expected": case_body.get("expected") or "",
            "critic_reason": (warning.get("explanation") or "").strip(),
            "code_snippets": snippets,
        })
    return out


def _warning_key(warning: dict) -> str:
    """Stable ID for a warning within a plan (ac_id + case title)."""
    ac_id = (warning.get("ac_id") or "").strip()
    title = (warning.get("missing_element") or "").strip()
    return f"{ac_id}::{title}"


CODE_CRITIC_SYSTEM_PROMPT = """You verify whether shipped code actually implements the behaviour a QA test case asserts.

You receive:
  1. A test case — its title, steps, expected outcome — and a note from an earlier critic explaining why the case's cited acceptance criterion (AC) doesn't cover this behaviour.
  2. Excerpts from the source repo the pull request touched: file paths and file contents (possibly truncated).

For each case, decide whether the code snippets show the behaviour the case asserts is present in the codebase.

**implemented** — the snippets contain identifiable evidence that the behaviour the case tests exists and works. Look for guards, branches, comments, constant names, method names, or config values that map to the case's assertion. Paraphrasing is fine; the code doesn't have to name things the same way the test does.

**not_implemented** — you searched the snippets and cannot find any code implementing the behaviour, or the code you did find contradicts the behaviour (e.g. the case asserts "empty result is not persisted" but the snippet writes without any empty-check).

**unclear** — the snippets are too tangential, truncated, or ambiguous to tell either way. Default to **unclear** when in doubt — it's better to leave the QA warning intact than to falsely reassure a tester.

Reply by calling the `report_code_grounding` tool with one entry per case. The `reason` field must be one short sentence. For **implemented**, name the specific file + symbol/comment/constant that anchors your call (e.g. "audio-cache.service.ts:520 has an explicit empty-buffer guard"). For **not_implemented**, name what you looked for and didn't find. For **unclear**, name what would have made it decidable."""


REPORT_CODE_GROUNDING_TOOL = {
    "name": "report_code_grounding",
    "description": (
        "Return an implemented/not_implemented/unclear verdict for each case, "
        "citing the snippet that anchors the call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "warning_key": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["implemented", "not_implemented", "unclear"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["warning_key", "verdict", "reason"],
                },
            },
        },
        "required": ["verdicts"],
    },
}


def build_code_critic_user_message(cases: list[dict]) -> str:
    """Render the verification payload as a deterministic user message.

    Deterministic ordering + no timestamps so tests can assert against the
    string directly.
    """
    lines: list[str] = [
        "For each test case below, decide whether the accompanying code snippets implement the behaviour the case asserts.",
        "Call the `report_code_grounding` tool with one verdict per case.",
        "",
    ]
    for case in cases:
        lines.append(f"── CASE {case['warning_key']} ──")
        lines.append(f"Title: {case['title']}")
        if case.get("steps"):
            lines.append("Steps:")
            for step in case["steps"]:
                lines.append(f"  - {step}")
        if case.get("expected"):
            lines.append(f"Expected: {case['expected']}")
        if case.get("critic_reason"):
            lines.append(f"AC critic said: {case['critic_reason']}")
        lines.append("Code snippets:")
        for snippet in case.get("code_snippets") or []:
            header = snippet["path"]
            if snippet.get("ref"):
                header += f" @ {snippet['ref']}"
            lines.append(f"  ── {header} ──")
            for src_line in snippet["content"].splitlines():
                lines.append(f"    {src_line}")
        lines.append("")
    return "\n".join(lines)


def parse_code_verdicts(raw: object) -> dict[str, dict]:
    """Coerce the LLM's tool output into ``{warning_key: {verdict, reason}}``.

    Malformed entries are dropped rather than raising — a broken critic
    should ship the un-downgraded warnings, not fail the request.
    """
    entries: object
    if isinstance(raw, dict):
        entries = raw.get("verdicts") or []
    else:
        entries = raw

    out: dict[str, dict] = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("warning_key")
        verdict = entry.get("verdict")
        if not isinstance(key, str) or not key.strip():
            continue
        if verdict not in ("implemented", "not_implemented", "unclear"):
            continue
        reason = entry.get("reason")
        out[key.strip()] = {
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
        }
    return out


def apply_code_verdicts(
    test_plan,
    verdicts: dict[str, dict],
    evidence_by_key: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Downgrade warnings whose behaviour the LLM confirmed lives in code.

    For each ``implemented`` verdict:
      - Set the matching warning's ``severity`` to ``"info"`` and rewrite
        its ``explanation`` to read as "beyond the AC but implemented in
        code — <critic reason>".
      - Attach a ``code_evidence`` block with the LLM's reason and the
        file paths that anchored the call, so QA can jump straight to the
        code.
      - Clear the matching case's ``needs_manual_verification`` flag so
        the "Ungrounded UI ref" badge disappears alongside the downgrade.

    ``not_implemented`` and ``unclear`` verdicts leave the warning
    untouched — QA still sees the original WARN entry.

    Returns the list of warnings that were downgraded (useful for logging).
    """
    downgraded: list[dict] = []
    if not verdicts:
        return downgraded

    warnings = getattr(test_plan, "grounding_warnings", None) or []
    if not warnings:
        return downgraded

    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        if warning.get("source") != "critic_ac":
            continue
        key = _warning_key(warning)
        verdict = verdicts.get(key)
        if not isinstance(verdict, dict):
            continue
        if verdict.get("verdict") != "implemented":
            continue

        reason = (verdict.get("reason") or "").strip() or (
            "code implements the behaviour despite the AC being silent."
        )
        warning["severity"] = "info"
        warning["explanation"] = (
            f"Beyond the cited AC, but the linked repo implements it: {reason}"
        )
        if evidence_by_key and key in evidence_by_key:
            files = [
                {"path": s.get("path", ""), "ref": s.get("ref", "")}
                for s in evidence_by_key[key]
                if isinstance(s, dict) and s.get("path")
            ]
            if files:
                warning["code_evidence"] = {
                    "reason": reason,
                    "files": files,
                }

        # Un-badge the matching case so it stops showing the
        # "Ungrounded UI ref" pill in the UI.
        located = _case_by_title(test_plan, warning.get("missing_element") or "")
        if located:
            _, _, case = located
            case["needs_manual_verification"] = False

        downgraded.append(warning)

    return downgraded
