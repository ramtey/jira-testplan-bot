"""Scope rules G, H and J, checked in code where code can tell.

SK-2822's plan 620 failed three of 34 cases, and the change under test caused
none of them. Two expected lowercase ``ENV=integ`` to be rejected "because the
filenames are case-sensitive". That holds on Linux only: operators run the
script on macOS, whose filesystem ignores case, so the unchanged
``[ -f ./env/flyway-$ENV.conf ]`` guard accepts it. The third failed the ticket
because three *other* repos still have the flaw the ticket fixed. The prompt
now has rules G–K for this (``SYSTEM_PROMPT``). This module catches the parts
a pattern can see, so a plan that ignores the rules still tells the tester.

Like the step check and the regression critic, everything here **marks** a case
where it stands and never moves or drops one. Moving a case changes the section
counts, and the counts are the progress key, so a move here would detach QA's
recorded results from the case they were recorded against.

* **G, platform:** an expected result that rests on filename case, GNU/BSD
  flags, line endings, path separators or the executable bit, in a case that
  names no platform, gets a marker saying the result depends on the OS.
* **H, pre-existing:** a case the model set ``pre_existing`` on is made to
  *say* so at the start of ``expected``. The flag alone is invisible in Jira.
* **J, out of scope:** a case whose expected result is cited from a repository
  that no pull request on the ticket changes gets a marker asking whether
  this change can make it pass at all. That is a question, not a verdict.
  A consumer of a changed API is a legitimate case in another repo, so a seam
  the extractor verified is never flagged.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")

PRE_EXISTING_LABEL = "Pre-existing behaviour, not changed by this ticket:"
PLATFORM_MARKER = (
    "Platform-dependent — this result differs between macOS and Linux and the "
    "case names neither; record which platform you ran on (rule G):"
)
SCOPE_MARKER = (
    "Check scope — this result rests on {repo}, which no pull request on this "
    "ticket changes; if this change cannot make it pass, it is a follow-up, "
    "not a case (rule J):"
)

KNOWN_FAILURE_MARKER = (
    "Not counted — this case expects to fail because of something the change "
    "does not do; it is a follow-up, not a result on this ticket (rule J):"
)

# Each trap is something that behaves differently on the macOS laptops operators
# use and the Linux machines CI uses. Filename case needs a file in the sentence:
# "the email match is case-insensitive" is about the app, not the host.
_FILE_WORDS = re.compile(r"\b(?:file|filename|files|path|director(?:y|ies)|folder)s?\b|\.\w{2,5}\b", re.I)
_TRAPS: tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    # "lowercase ENV=integ ... exits 1" asserts the same thing without the word.
    ("filename case", re.compile(r"\bcase[- ]?(?:in)?sensitiv|\blower[- ]?case\b|\bupper[- ]?case\b", re.I),
     _FILE_WORDS),
    ("GNU vs BSD flags", re.compile(
        r"\bsed\s+-i\b|\breadlink\s+-f\b|\bgrep\s+-P\b|\bdate\s+-d\b|\bstat\s+-c\b|\bxargs\s+-r\b"), None),
    ("line endings", re.compile(r"\bCRLF\b|\bline[- ]endings?\b|\\r\\n", re.I), None),
    ("path separators", re.compile(r"\b[A-Z]:\\\\|\bdrive letters?\b|\bpath separators?\b", re.I), None),
    ("executable bit", re.compile(
        r"\bexecutable bit\b|\bchmod\s+\+x\b|\bfile mode\b|\bexecute permission\b|\b(?:is|be|not|isn't)\s+executable\b",
        re.I), None),
)
_PLATFORM_NAMED = re.compile(
    r"\b(?:macOS|mac\s?OS|OS\s?X|darwin|APFS|Linux|Ubuntu|Debian|Alpine|Windows|WSL|GNU|BSD)\b"
    r"|\b(?:on|in) (?:the )?CI\b",
    re.I,
)
# A case that says it will fail because of something the change did not do is
# a gap written as a case. SK-2822's second plan added one ("Expect this case to
# FAIL against the current code — it is a scope gap").
# Only a case that says, about ITSELF, that it is expected to fail. Broader
# phrasings were tried and were wrong: a bare "scope gap" matched a case that
# followed rule J ("... is the separate scope gap, NOT a failure of this change"),
# and "not counted against this ticket" matched a legitimate cleanDisabled case
# that used it for one recording step. Deciding that a whole case does not count
# needs judgement, so code only marks the explicit form and never moves anything.
_KNOWN_FAILURE = re.compile(
    r"\bexpect(?:ed)? this (?:case|check) to fail\b"
    r"|\bmark (?:this (?:case )?)?failed unless\b",
    re.I,
)
_PR_REPO = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/", re.I)
_SOURCE_REPO = re.compile(r"^\s*`?([\w.-]+)/([\w.-]+):(?=\S)")


def _cases(test_plan: Any) -> Iterable[tuple[str, int, dict]]:
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


def _case_text(case: dict) -> str:
    parts = [case.get("title"), case.get("preconditions"), case.get("expected"),
             case.get("environment"), case.get("test_data"), *(case.get("steps") or [])]
    return " ".join(p for p in parts if isinstance(p, str))


def platform_traps(text: str) -> list[str]:
    """The traps in ``text``, empty when there are none or a platform is named."""
    if not text or _PLATFORM_NAMED.search(text):
        return []
    found = []
    for name, pattern, needs in _TRAPS:
        if pattern.search(text) and (needs is None or needs.search(text)):
            found.append(name)
    return found


def _prefix(text: str, marker: str) -> str:
    text = text or ""
    return text if text.startswith(marker) else f"{marker} {text}".rstrip()


def flag_platform_assumptions(test_plan: Any) -> list[dict]:
    """Rule G. Mark expected results that depend on the host OS without naming it.

    A trap is looked for in the expected result (the claim); whether a platform
    is named is looked for in the whole case, because "On macOS" in the
    preconditions scopes the expectation just as well.
    """
    flagged: list[dict] = []
    for section, i, case in _cases(test_plan):
        expected = case.get("expected") or ""
        traps = [t for t in platform_traps(expected) if not _PLATFORM_NAMED.search(_case_text(case))]
        if traps:
            case["expected"] = _prefix(expected, PLATFORM_MARKER)
            flagged.append({"location": f"{section}[{i}]", "traps": traps})
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        if isinstance(line, str):
            traps = platform_traps(line)
            if traps and PLATFORM_MARKER not in line:
                lines[i] = _insert_after_emoji(line, PLATFORM_MARKER)
                flagged.append({"location": f"regression_checklist[{i}]", "traps": traps})
    return flagged


def _insert_after_emoji(line: str, marker: str) -> str:
    """Put ``marker`` after a leading priority emoji, so the line still sorts."""
    m = re.match(r"^(\s*(?:🔴|🟡|🟢)\s*)", line)
    head = m.group(1) if m else ""
    return f"{head}{marker} {line[len(head):]}"


def enforce_pre_existing_label(test_plan: Any) -> int:
    """Rule H. A ``pre_existing`` case says so where every renderer shows it."""
    labelled = 0
    for _section, _i, case in _cases(test_plan):
        if case.get("pre_existing") is True:
            expected = case.get("expected") or ""
            if PRE_EXISTING_LABEL.lower() not in expected.lower():
                case["expected"] = f"{PRE_EXISTING_LABEL} {expected}".rstrip()
                labelled += 1
    return labelled


def changed_repos(dev_infos: Iterable[dict | None]) -> set[str]:
    """``owner/repo`` (lower-cased) of every pull request the plan was built from."""
    repos: set[str] = set()
    for dev in dev_infos:
        for pr in (dev or {}).get("pull_requests") or []:
            if isinstance(pr, dict):
                m = _PR_REPO.search(pr.get("url") or "")
                if m:
                    repos.add(m.group(1).lower())
    return repos


def flag_out_of_scope_cases(test_plan: Any, repos: set[str]) -> list[dict]:
    """Rule J. Mark cases whose expected result is cited from an unchanged repo.

    Only an ``owner/repo:path`` citation whose owner is one of the PRs' owners
    is read as a repository, so a bare ``src/auth.ts:64`` is never mistaken
    for one. With no PR repos known there is nothing to compare, so nothing is
    flagged.
    """
    if not repos:
        return []
    owners = {r.split("/", 1)[0] for r in repos}
    flagged: list[dict] = []
    for section, i, case in _cases(test_plan):
        m = _SOURCE_REPO.match(case.get("expected_source") or "")
        if not m:
            continue
        owner, name = m.group(1).lower(), m.group(2).lower()
        repo = f"{owner}/{name}"
        if owner not in owners or repo in repos:
            continue
        seam = case.get("seam") if isinstance(case.get("seam"), dict) else {}
        if seam.get("verified") is True:
            continue
        marker = SCOPE_MARKER.format(repo=f"{m.group(1)}/{m.group(2)}")
        case["expected"] = _prefix(case.get("expected") or "", marker)
        flagged.append({"location": f"{section}[{i}]", "repo": repo})
    return flagged


def flag_known_failures(test_plan: Any) -> list[dict]:
    """Rule J. Mark a case whose expected result says it will fail for a reason
    outside the change. It is a gap, and a tester marking it failed would score
    the ticket for something it never claimed to do."""
    flagged: list[dict] = []
    for section, i, case in _cases(test_plan):
        expected = case.get("expected") or ""
        if _KNOWN_FAILURE.search(expected) and KNOWN_FAILURE_MARKER not in expected:
            case["expected"] = f"{KNOWN_FAILURE_MARKER} {expected}"
            flagged.append({"location": f"{section}[{i}]", "repo": None})
    return flagged


def run_scope_checks(test_plan: Any, dev_infos: Iterable[dict | None]) -> dict:
    """All three, in the order that keeps the markers readable: the label the
    model chose first, then what code noticed."""
    return {
        "pre_existing_labelled": enforce_pre_existing_label(test_plan),
        "platform_unscoped": flag_platform_assumptions(test_plan),
        "out_of_scope_suspects": flag_out_of_scope_cases(test_plan, changed_repos(dev_infos))
        + flag_known_failures(test_plan),
    }


# ---------------------------------------------------------------------------
# Rule G critic: rewrite what the detector flags, in place
# ---------------------------------------------------------------------------
#
# Marking a platform assumption makes it visible, but the expected result is
# still wrong. On SK-2822 the generator was given rule G and still wrote
# "lowercase ENV=integ is rejected" in two regenerations out of two. It
# anchored on a reviewer comment that said "the committed filenames are
# case-sensitive". So the cases the detector flags go to a narrow critic that
# rewrites them where they stand. Same id, same thing checked; only the
# platform claim changes. A case the critic leaves alone, or cannot reach,
# keeps its marker.

PLATFORM_CRITIC_SYSTEM_PROMPT = """You fix one kind of mistake in QA test cases: an expected result that depends on the host operating system but does not say which one.

Facts to apply unless the case or code says otherwise:
- Operators and developers run commands on macOS: zsh, APFS (case-INSENSITIVE filenames: `[ -f ./conf/app-integ.conf ]` finds `app-Integ.conf`), BSD coreutils (`sed -i ''`, no `readlink -f` before macOS 12.3, no `grep -P`, `date -v` not `date -d`).
- CI runs on Linux: bash, case-SENSITIVE filenames, GNU coreutils.

For each case you are given, return a rewrite that:
1. Keeps checking the same behaviour, so it stays the same test at the same id. Do not add or remove steps unless a step itself makes the platform claim.
2. Gives the expected result per platform ("On macOS …; on Linux CI …") when they differ, OR restricts the claim to what holds on both. Derive each platform's result from the code shown. If the code does not settle it, say what to RECORD rather than asserting an outcome.
3. Drops a reason that the case itself says was already fixed (e.g. "a reviewer found the doc listed lowercase names" when the same commit corrected the doc). Never keep quoting a fixed finding as the reason.
4. Keeps every prefix the text starts with exactly as it is ("🔴", "Deploy check first: …", "Unverified — assumption …:"), and keeps file:line citations.
5. Names a platform somewhere in the rewritten text.
6. When the platform claim is an aside that the case does not test (e.g. "names the arguments Integ, Staging, Prod (matching the case-sensitive filenames)" on a check of a document's wording), DELETE the aside and add only a short "(same on macOS and Linux)". Do not expand an aside into a per-platform section.

If a case does not actually depend on the platform, return action "keep". Call report_platform_rewrites exactly once."""

REPORT_PLATFORM_REWRITES_TOOL = {
    "name": "report_platform_rewrites",
    "description": "Report a rewrite (or keep) for every case id you were given.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rewrites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "action": {"type": "string", "enum": ["rewrite", "keep"]},
                        "text": {"type": "string", "description": "The rewritten expected result, or the rewritten regression line. Required for rewrite."},
                        "steps": {"type": "array", "items": {"type": "string"},
                                  "description": "Only when a step itself made the platform claim: the full step list, same length and order."},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "action"],
                },
            }
        },
        "required": ["rewrites"],
    },
}

_CODE_CHARS_PER_FILE = 4000
_CODE_CHARS_TOTAL = 12000


def _patches(dev_infos: Iterable[dict | None]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dev in dev_infos:
        for pr in (dev or {}).get("pull_requests") or []:
            if not isinstance(pr, dict):
                continue
            for f in pr.get("files_changed") or []:
                if isinstance(f, dict) and f.get("filename") and f.get("patch"):
                    out.setdefault(f["filename"], f["patch"])
    return out


def collect_platform_items(test_plan: Any, dev_infos: Iterable[dict | None]) -> list[dict]:
    """The flagged cases, each with the diff hunks of the files it names."""
    patches = _patches(dev_infos)
    items: list[dict] = []
    for section, i, case in _cases(test_plan):
        if platform_traps(case.get("expected") or "") and not _PLATFORM_NAMED.search(_case_text(case)):
            items.append({"id": f"{section}:{i}", "title": case.get("title"),
                          "steps": case.get("steps") or [], "text": case.get("expected") or "",
                          "_case": case})
    for i, line in enumerate(getattr(test_plan, "regression_checklist", None) or []):
        if isinstance(line, str) and platform_traps(line):
            items.append({"id": f"regression_checklist:{i}", "text": line})
    budget = _CODE_CHARS_TOTAL
    for item in items:
        blob = " ".join([item["text"], item.get("title") or "", *item.get("steps", [])])
        code = {}
        for name, patch in patches.items():
            if budget <= 0:
                break
            base = name.rsplit("/", 1)[-1]
            if name in blob or base in blob:
                code[name] = patch[:_CODE_CHARS_PER_FILE]
                budget -= len(code[name])
        item["code"] = code
    return items


def build_platform_critic_message(items: list[dict]) -> str:
    parts = []
    for item in items:
        parts.append(f"### {item['id']}")
        if item.get("title"):
            parts.append(f"Title: {item['title']}")
        if item.get("steps"):
            parts.append("Steps:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(item["steps"], 1)))
        parts.append(("Expected result" if item.get("title") else "Regression line") + f":\n{item['text']}")
        for name, patch in (item.get("code") or {}).items():
            parts.append(f"Diff of {name} at the commit under test:\n```\n{patch}\n```")
    return "\n\n".join(parts)


def parse_platform_rewrites(raw: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in (raw or {}).get("rewrites") or []:
        if isinstance(r, dict) and r.get("action") == "rewrite" and isinstance(r.get("text"), str) and r["text"].strip():
            out[str(r.get("id"))] = r
    return out


def apply_platform_rewrites(test_plan: Any, items: list[dict], rewrites: dict[str, dict]) -> list[str]:
    """Apply each rewrite at its own id. Never changes how many cases or lines a
    section has, and refuses a rewrite that still names no platform."""
    applied: list[str] = []
    lines = getattr(test_plan, "regression_checklist", None) or []
    for item in items:
        r = rewrites.get(item["id"])
        if not r or not _PLATFORM_NAMED.search(r["text"]):
            continue
        if "_case" in item:
            case = item["_case"]
            case["expected"] = r["text"].strip()
            steps = r.get("steps")
            if isinstance(steps, list) and len(steps) == len(case.get("steps") or []) and all(isinstance(x, str) for x in steps):
                case["steps"] = steps
            case["platform_rewritten"] = r.get("reason") or True
        else:
            index = int(item["id"].rsplit(":", 1)[1])
            if index < len(lines):
                lines[index] = r["text"].strip()
        applied.append(item["id"])
    return applied


async def run_platform_critic(llm: Any, test_plan: Any, dev_infos: Iterable[dict | None]) -> tuple[list[str], str | None]:
    """Rule G, rewritten rather than only marked. Returns ``(rewritten ids, gap)``.
    ``gap`` is set when the critic could not run, so the plan says the absence
    of rewrites is not a clean check."""
    dev_infos = list(dev_infos)
    items = collect_platform_items(test_plan, dev_infos)
    if not items:
        return [], None
    rewrite = getattr(llm, "rewrite_platform_assumptions", None)
    if rewrite is None:
        return [], None
    payload = [{k: v for k, v in item.items() if k != "_case"} for item in items]
    rewrites = await rewrite(payload)
    if rewrites is None:
        return [], f"the platform critic could not run, so {len(items)} platform-dependent case(s) are only marked"
    return apply_platform_rewrites(test_plan, items, rewrites), None
