"""Rule P: every action step names an input the case's surface really has.

SK-2948 (plan 655) had two mobile cases, "County-only quote (no city)" and
"Same Utah example run twice — with a city and without", whose steps said
"leave the city blank". The server's formula path runs when no city is sent,
and the generator wrote the case on the surface the rest of the plan used.
But the mobile app takes an address only as a full street address picked
from autocomplete. There is no city field to leave blank, so no tester could
ever perform the step. Only the web app has separate city and county fields.
Nothing downstream asked whether the surface offers the input:
step_grounding checks that a named control is in the changed code, and
reachability (rule L) checks that *some* trigger reaches the branch. Neither
asks whether *this surface* can send that trigger.

What this pass does
-------------------
Every web_ui and mobile case goes to a critic that can search and read the
whole org (the code explorer's tools). For each case it:

1. finds the client app behind the surface and the screens and inputs that
   app actually exposes (form fields, pickers, autocomplete limits, and
   fallbacks such as "no address means use the profile county");
2. checks every action step against them, citing the file each input is in;
3. for a case aimed at a server branch, lists every client that can trigger
   that branch and how, and when the case's surface cannot, gives the cases
   that can: the same intent on another surface, the real route on this one
   ("set the service area, then leave the address empty"), or backend_http.

What code enforces, not the model
---------------------------------
A citation counts only if the critic read that file in this run
(``Workspace.read_files``). So:

* "performable" stands only when every action step cites an input in a file
  it read. A step it did not back that way is marked "Unverified — not checked
  against <client>'s inputs" in place. It is never silently passed.
* "not performable" changes a case only when the claim cites a file the critic
  read. A replacement is used only when each of its action steps does too.
  An uncited "not performable" is marked, never acted on: losing a good case
  because of a guess costs more than one marked step.
* With one valid replacement, the case is rewritten in place. With several,
  it is split, one case per surface, each with the steps that surface can do.
  A case none of them can do leaves the checklist for the code-review /
  unit-test list (``case_disposition.route_out_of_uat``), and the reason goes
  with it.
* A case QA already passed (by title) is only marked. A recorded result keeps
  describing the case it was recorded on.

Every changed case keeps the original surface, the original steps and the
reason in ``surface_check``, so nothing is quietly relabelled. If the critic
could not run, the plan says so in ``critics_unavailable``. A check that did
not happen is never reported as a pass.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from .code_exploration import Workspace, _new_result, run_tool_loop
from .diff_budget import allocate_patch_budget

logger = logging.getLogger(__name__)

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests")
#: Surfaces whose cases a tester drives through a client app's own inputs.
CHECKED_SURFACES = frozenset({"web_ui", "mobile"})
#: Surfaces a replacement may move a case to.
ALLOWED_SURFACES = frozenset({"web_ui", "mobile", "backend_http"})

NOT_PERFORMABLE_MARKER = "Not performable on {client} — {why} (rule P):"
NOT_OBSERVABLE_MARKER = "Not observable on {client} — {why} (rule P):"
UNCHECKED_MARKER = "Unverified — not checked against {client}'s inputs (rule P):"
_MARKED_RE = re.compile(r"^\s*(?:Not performable on |Not observable on |Unverified — )", re.I)

#: Steps code writes (deploy checks) or that only observe are not actions on an input.
_CODE_STEP_RE = re.compile(r"^\s*(?:Build check|Deploy check)\b", re.I)

MAX_TURNS = 8
MAX_SEARCHES = 10
MAX_READS = 14
_DIFF_BUDGET = 20_000

#: Fields that describe the old steps. Their step numbers mean nothing once the steps change.
_STEP_FIELDS = ("step_grounding", "unverified_steps", "note_backed_steps", "surface_inputs")


def _cases(test_plan: Any) -> Iterable[tuple[str, int, dict]]:
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


def collect_surface_items(test_plan: Any) -> list[dict]:
    """The cases a tester drives through a client app: web_ui and mobile, still runnable."""
    items = []
    for section, i, case in _cases(test_plan):
        if case.get("surface") not in CHECKED_SURFACES or case.get("covered_by_unit_test"):
            continue
        if isinstance(case.get("reachability"), dict) and case["reachability"].get("reachable") is False:
            continue
        items.append({"id": f"{section}:{i}", "section": section, "_case": case})
    return items


# ---------------------------------------------------------------------------
# The critic
# ---------------------------------------------------------------------------

SURFACE_SYSTEM_PROMPT = """You check one thing about manual QA test cases: can a tester perform every step on the surface the case names?

A plan is written from a diff, usually on the server. The generator picks a surface (mobile, web_ui) and writes steps in terms of what the SERVER needs ("send no city", "omit the county"). A client app only sends what its own screens let a user enter. If the surface has no input for a step, the step cannot be performed. The case fails even though the code is fine.

For each case:
1. Find the client app behind its surface: the mobile app for `mobile`, the web app the steps name for `web_ui`. Search the org for its repo and screens (search_code). The PRs below may not contain it.
2. Find the screens and inputs that app really exposes for the case's steps: form fields, pickers, toggles, and the limits on them (an address that can only be picked from autocomplete, a required field, a field the screen does not have). Also find its fallbacks: what it sends when an input is left empty, for example the profile's county or a saved service area. Read the files with read_file. A search hit alone is not a read.
3. For every step, say whether it is an "action" (the tester enters, picks, taps or leaves something empty) or an "observe" step (reads a result). For every action step, name the input it uses and cite `repo:path:line` from a file you READ. A step that needs an input the surface does not have is not performable. Say why, citing the file that shows the limit. An observe step asking for something this surface never shows (a debug-only banner, a field the screen does not render) is `performable: false` too, with the file that shows it.
4. When a case targets a server branch (a code path that runs only for some request shape, for example "the formula path when no city is sent"), list every client that can send that shape and how (`clients`), including clients the case does not use. Cite the file for each.
5. If the case's surface cannot perform it, give `alternatives`, in this order of preference:
   - the same intent on a surface whose client CAN send that shape (for example a web app with separate city and county fields), with steps naming that client's inputs;
   - the real route on the same surface, if one exists (for example "set the service area in the profile, then leave the property address empty", when the app falls back to the profile's county);
   - `backend_http`, a direct request with that shape, citing the route or handler you read.
   Give one alternative per surface that can do it. Each alternative's steps are the complete list for that surface. Every action step names an input and cites the file it is in, in `inputs`. Copy any step that is not an input action (setup, opening results, recording values) and keep it on that surface. Drop or replace an observe step that surface cannot show.
   If no surface can produce it, give no alternatives and give `suggested_test`: the unit test that would cover the branch.

Verdicts:
- "performable": every action step names an input this surface has, with a citation from a file you read.
- "not_performable": at least one action step needs an input this surface does not have. `steps` must name it, with the file that shows the limit.
- "unsure": you could not find or read the client's code for this (budget spent, repo not found). Never answer "performable" for a step you did not check.

Judge only whether the surface can perform the steps, never whether the code is correct. Spend searches on steps that shape a request (address, empty fields, optional inputs, file types, dates). A plain "open the results" needs none. Call report_surface_inputs exactly once, with one verdict per case id."""

_STEP_ITEM = {
    "type": "object",
    "properties": {
        "step": {"type": "integer", "description": "1-based step number in the case as given."},
        "kind": {"type": "string", "enum": ["action", "observe"]},
        "input": {"type": "string", "description": "The input, field or control the step uses on this surface."},
        "source": {"type": "string", "description": "repo:path:line of the file you read that shows it (or its absence/limit)."},
        "performable": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["step", "kind", "performable"],
}

REPORT_SURFACE_TOOL = {
    "name": "report_surface_inputs",
    "description": "Report, for every case id you were given, whether its surface can perform every step.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["performable", "not_performable", "unsure"]},
                        "client": {"type": "string", "description": "The client app behind this case's surface, e.g. 'the mobile app (acme/mobile)'."},
                        "reason": {"type": "string"},
                        "steps": {"type": "array", "items": _STEP_ITEM},
                        "server_branch": {"type": "string", "description": "The server code path the case targets, if any, with file:line."},
                        "clients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "surface": {"type": "string"},
                                    "client": {"type": "string"},
                                    "can_trigger": {"type": "boolean"},
                                    "how": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                                "required": ["surface", "client", "can_trigger"],
                            },
                        },
                        "alternatives": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "surface": {"type": "string", "enum": sorted(ALLOWED_SURFACES)},
                                    "client": {"type": "string"},
                                    "steps": {"type": "array", "items": {"type": "string"}},
                                    "preconditions": {"type": "string"},
                                    "expected_note": {"type": "string", "description": "Only when the original expected result names UI this surface does not show: what to read instead. The original expected result is kept."},
                                    "trigger": {"type": "string"},
                                    "inputs": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "step": {"type": "integer"},
                                                "input": {"type": "string"},
                                                "source": {"type": "string"},
                                            },
                                            "required": ["step", "input", "source"],
                                        },
                                    },
                                },
                                "required": ["surface", "client", "steps", "inputs"],
                            },
                        },
                        "suggested_test": {"type": "string"},
                    },
                    "required": ["id", "verdict", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def build_surface_message(items: list[dict], pull_requests: list[dict],
                          exploration: dict | None) -> str:
    parts = ["## CASES"]
    for item in items:
        c = item["_case"]
        steps = "\n".join(f"  {n}. {s}" for n, s in enumerate(c.get("steps") or [], 1))
        declared = c.get("surface_inputs") or []
        decl = ("\nDeclared inputs (the generator's own, unchecked): "
                + json.dumps(declared, ensure_ascii=False)) if declared else ""
        parts.append(
            f"### {item['id']} (surface: {c.get('surface')})\nTitle: {c.get('title')}\n"
            f"Preconditions: {c.get('preconditions') or '-'}\n"
            f"Trigger (as the plan states it): {c.get('trigger') or '(none given)'}\n"
            f"Steps:\n{steps}\nExpected: {c.get('expected')}{decl}"
        )
    parts.append("## PULL REQUESTS AND DIFFS (may be cut to fit; the client apps may not be here)")
    per_pr = _DIFF_BUDGET // max(1, len(pull_requests))
    for pr in pull_requests:
        parts.append(f"- {pr.get('repository')} #{pr.get('number')}: {pr.get('title') or ''}")
        for name, patch, cut in allocate_patch_budget(pr.get("files_changed") or [],
                                                      total_budget=per_pr, per_file_cap=5000):
            parts.append(f"--- {pr.get('repository')}:{name}{' (cut)' if cut else ''}\n{patch}")
    findings = (exploration or {}).get("findings") or []
    if findings:
        parts.append("## WHAT THE CODE EXPLORER FOUND OUTSIDE THE DIFFS")
        for f in findings:
            parts.append(f"- [{f.get('kind')}] {f.get('repo')}:{f.get('path')}:{f.get('lines')} — "
                         f"{f.get('what_it_does')}")
    return "\n\n".join(parts)


def parse_surface_verdicts(raw: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for v in (raw or {}).get("verdicts") or []:
        if isinstance(v, dict) and v.get("verdict") in ("performable", "not_performable", "unsure"):
            out[str(v.get("id"))] = v
    return out


# ---------------------------------------------------------------------------
# Applying verdicts: code decides what a citation is worth
# ---------------------------------------------------------------------------


def _split_citation(source: object) -> tuple[str, str] | None:
    """``owner/repo:path[:line[-line]][ note]`` → ``(owner/repo, path)``.

    The critic often appends a note (``index.tsx (testID "main-action-card")``).
    That is fine: what counts is whether the file itself was read.
    """
    if not isinstance(source, str):
        return None
    m = re.match(r"^\s*([\w.-]+/[\w.-]+):(\S+?)(?::\d+(?:-\d+)?)?(?:\s.*)?$", source)
    return (m.group(1), m.group(2).lstrip("./")) if m else None


def cited_read(source: object, read_files: set[tuple[str, str]]) -> bool:
    """Whether a citation names a file the critic actually read in this run."""
    parts = _split_citation(source)
    if not parts:
        return False
    repo, path = parts
    return any(r == repo and (p == path or p.endswith("/" + path)) for r, p in read_files)


def _is_action_step(text: object) -> bool:
    return isinstance(text, str) and not _CODE_STEP_RE.match(text)


def _prefix(text: str, marker: str) -> str:
    return text if _MARKED_RE.match(text) else f"{marker} {text}".rstrip()


def _client(v: dict, case: dict) -> str:
    return (v.get("client") or "").strip() or f"the {case.get('surface')} surface"


def _step_reports(v: dict) -> dict[int, dict]:
    out = {}
    for s in v.get("steps") or []:
        if isinstance(s, dict) and isinstance(s.get("step"), int) and not isinstance(s.get("step"), bool):
            out[s["step"]] = s
    return out


def _unbacked_action_steps(case: dict, reports: dict[int, dict],
                           read_files: set[tuple[str, str]]) -> list[int]:
    """Action steps with no performable report citing a file that was read."""
    out = []
    for n, text in enumerate(case.get("steps") or [], 1):
        if not _is_action_step(text):
            continue
        r = reports.get(n)
        if r and r.get("kind") == "observe":
            continue
        if r and r.get("performable") is True and cited_read(r.get("source"), read_files):
            continue
        out.append(n)
    return out


def _mark_unobservable(case: dict, reports: dict[int, dict], read_files: set[tuple[str, str]],
                       client: str) -> list[int]:
    """Mark observe steps the critic showed, from a file it read, this surface never displays."""
    marked = []
    for n, r in sorted(reports.items()):
        if r.get("kind") != "observe" or r.get("performable") is not False:
            continue
        if not cited_read(r.get("source"), read_files) or not 1 <= n <= len(case.get("steps") or []):
            continue
        why = (r.get("why") or "this surface does not show it").strip()
        case["steps"][n - 1] = _prefix(case["steps"][n - 1], NOT_OBSERVABLE_MARKER.format(client=client, why=why))
        marked.append(n)
    return marked


def _working_route(v: dict, read_files: set[tuple[str, str]]) -> dict | None:
    """A client the critic showed, from a file it read, CAN send the shape the case needs."""
    for c in v.get("clients") or []:
        if isinstance(c, dict) and c.get("can_trigger") is True and cited_read(c.get("source"), read_files):
            return c
    return None


def _valid_alternative(alt: dict, read_files: set[tuple[str, str]]) -> bool:
    """A replacement counts when every action step it has cites an input in a file read."""
    if not isinstance(alt, dict) or alt.get("surface") not in ALLOWED_SURFACES:
        return False
    steps = [s for s in alt.get("steps") or [] if isinstance(s, str) and s.strip()]
    if not steps:
        return False
    inputs = {i.get("step"): i for i in alt.get("inputs") or [] if isinstance(i, dict)}
    if not inputs or not all(cited_read(i.get("source"), read_files) for i in inputs.values()):
        return False
    # Every input it cites must point at one of its own steps.
    return all(isinstance(n, int) and 1 <= n <= len(steps) for n in inputs)


def _owner(pull_requests: list[dict]) -> str:
    repos = sorted({pr.get("repository") for pr in pull_requests if pr.get("repository")})
    return f"Owners of {', '.join(repos)}" if repos else "The team that owns the changed code"


def _rewrite(case: dict, alt: dict, record: dict, *, split_of: int | None) -> dict:
    out = {k: v for k, v in case.items() if k not in _STEP_FIELDS}
    steps = [s.strip() for s in alt["steps"] if isinstance(s, str) and s.strip()]
    out.update(
        surface=alt["surface"],
        steps=steps,
        surface_inputs=[{"step": i["step"], "input": i.get("input") or "", "source": i["source"]}
                        for i in alt.get("inputs") or [] if isinstance(i, dict)],
        surface_check={**record, "status": "rewritten" if split_of is None else "split",
                       "new_surface": alt["surface"], "client": alt.get("client") or ""},
    )
    if split_of is not None:
        out["surface_check"]["split_count"] = split_of
        out["title"] = f"{case.get('title') or ''} — {alt.get('client') or alt['surface']}".strip(" —")
    for k in ("preconditions", "trigger"):
        if isinstance(alt.get(k), str) and alt[k].strip():
            out[k] = alt[k].strip()
    # The pass criterion is never taken from the critic: it judges whether a
    # surface can perform the steps, not what the change should do (a live run
    # put "drop the fee-source step" into this field). Its note is kept beside.
    note = alt.get("expected_note") or alt.get("expected")
    if isinstance(note, str) and note.strip():
        out["surface_check"]["expected_note"] = note.strip()
    if alt["surface"] == "backend_http":
        out.pop("needs_manual_verification", None)
    return out


def apply_surface_verdicts(test_plan: Any, items: list[dict], verdicts: dict[str, dict],
                           read_files: set[tuple[str, str]], *,
                           protected_titles: frozenset[str] = frozenset(),
                           pull_requests: list[dict] | None = None) -> dict:
    """Apply each verdict. Returns what changed, by the case's id when it was checked.

    Rewrites and splits happen per section from the end, so an inserted split
    does not shift the ids of the items still to apply.
    """
    changes: dict[str, list] = {"performable": [], "rewritten": [], "split": [], "routed_out": [],
                                "marked": [], "unchecked": []}
    reasons: dict[str, str] = {}
    replacements: dict[tuple[str, int], list[dict]] = {}

    for item in items:
        case, cid = item["_case"], item["id"]
        v = verdicts.get(cid)
        client = _client(v or {}, case)
        reports = _step_reports(v or {})
        if not v or v["verdict"] == "unsure":
            _mark_unobservable(case, reports, read_files, client)
            unbacked = _unbacked_action_steps(case, reports, read_files)
            for n in unbacked:
                case["steps"][n - 1] = _prefix(case["steps"][n - 1], UNCHECKED_MARKER.format(client=client))
            case["surface_check"] = {"status": "unchecked", "client": client,
                                     "reason": (v or {}).get("reason") or "the critic gave no verdict for this case",
                                     "steps": unbacked}
            changes["unchecked"].append(cid)
            continue

        failing = [r for r in reports.values() if r.get("kind") == "action" and r.get("performable") is False]
        backed_failing = [r for r in failing if cited_read(r.get("source"), read_files)]
        if v["verdict"] == "not_performable" and backed_failing:
            reason = (v.get("reason") or "").strip() or "; ".join(
                (r.get("why") or "").strip() for r in backed_failing if r.get("why")) or \
                "the surface has no input for this step"
            record = {"original_surface": case.get("surface"), "original_steps": list(case.get("steps") or []),
                      "original_title": case.get("title"), "reason": reason, "client": client,
                      "not_performable_steps": [{"step": r["step"], "input": r.get("input") or "",
                                                 "why": r.get("why") or "", "source": r.get("source")}
                                                for r in backed_failing],
                      "clients": [c for c in v.get("clients") or [] if isinstance(c, dict)],
                      "server_branch": (v.get("server_branch") or "").strip()}
            reasons[cid] = reason
            if (case.get("title") or "") in protected_titles:
                for r in backed_failing:
                    n = r["step"]
                    if 1 <= n <= len(case.get("steps") or []):
                        why = (r.get("why") or reason).strip()
                        case["steps"][n - 1] = _prefix(case["steps"][n - 1],
                                                       NOT_PERFORMABLE_MARKER.format(client=client, why=why))
                _mark_unobservable(case, reports, read_files, client)
                case["surface_check"] = {**record, "status": "marked_passed_case"}
                changes["marked"].append(cid)
                continue
            alts = [a for a in v.get("alternatives") or [] if _valid_alternative(a, read_files)]
            # One case per surface: a second alternative on the same surface is a duplicate.
            seen, unique = set(), []
            for a in alts:
                key = (a["surface"], (a.get("client") or "").strip().lower())
                if key not in seen:
                    seen.add(key)
                    unique.append(a)
            section, idx = cid.split(":")
            if len(unique) == 1:
                replacements[(section, int(idx))] = [_rewrite(case, unique[0], record, split_of=None)]
                changes["rewritten"].append(cid)
            elif unique:
                replacements[(section, int(idx))] = [_rewrite(case, a, record, split_of=len(unique))
                                                     for a in unique]
                changes["split"].append(cid)
            elif (route := _working_route(v, read_files)) is not None:
                # A client can send the shape, but the critic gave no usable
                # rewrite. Dropping the case would lose a test that can be run,
                # so keep it, mark the step, and name the route that works.
                how = (route.get("how") or "").strip() or "see the cited file"
                for r in backed_failing:
                    n = r["step"]
                    if 1 <= n <= len(case.get("steps") or []):
                        why = (r.get("why") or reason).strip()
                        case["steps"][n - 1] = _prefix(
                            case["steps"][n - 1],
                            NOT_PERFORMABLE_MARKER.format(client=client, why=why)
                            + f" Use instead, on {route.get('client') or route.get('surface')}: {how}"
                              f" ({route.get('source')}).")
                case["needs_manual_verification"] = True
                _mark_unobservable(case, reports, read_files, client)
                case["surface_check"] = {**record, "status": "marked_route_exists",
                                         "working_route": route}
                changes["marked"].append(cid)
            else:
                # Nothing a tester can do produces it: out of the checklist, reason attached.
                case["reachability"] = {
                    "reachable": False,
                    "reason": f"No surface can perform this case (rule P): {reason}",
                    "original_title": case.get("title"), "original_expected": case.get("expected"),
                    "owner": _owner(pull_requests or []),
                    "suggested_ticket": f"Add a unit test for: {case.get('title')}",
                    **({"suggested_test": v["suggested_test"].strip()}
                       if (v.get("suggested_test") or "").strip() else {}),
                }
                case["surface_check"] = {**record, "status": "routed_out"}
                changes["routed_out"].append(cid)
            continue

        # "performable", or a "not_performable" that cited nothing it read.
        unobservable = _mark_unobservable(case, reports, read_files, client)
        unbacked = _unbacked_action_steps(case, reports, read_files)
        uncited_claim = v["verdict"] == "not_performable"
        for n in unbacked:
            r = reports.get(n) or {}
            if uncited_claim and r.get("performable") is False:
                why = (r.get("why") or v.get("reason") or "").strip()
                marker = (f"Unverified — the surface check says {client} cannot do this ({why}) "
                          "but cited no file it read (rule P):")
            else:
                marker = UNCHECKED_MARKER.format(client=client)
            case["steps"][n - 1] = _prefix(case["steps"][n - 1], marker)
        if unbacked or unobservable:
            case["surface_check"] = {"status": "partly_checked", "client": client, "steps": unbacked,
                                     "reason": (v.get("reason") or "").strip(),
                                     **({"not_observable_steps": unobservable} if unobservable else {})}
            changes["unchecked" if unbacked else "marked"].append(cid)
        else:
            case["surface_check"] = {"status": "performable", "client": client}
            case.setdefault("surface_inputs", [
                {"step": n, "input": r.get("input") or "", "source": r.get("source")}
                for n, r in sorted(reports.items()) if r.get("kind") == "action"])
            changes["performable"].append(cid)

    for (section, idx) in sorted(replacements, key=lambda k: (k[0], -k[1])):
        cases = getattr(test_plan, section)
        cases[idx:idx + 1] = replacements[(section, idx)]
    if reasons:
        changes["reasons"] = reasons
    return changes


async def run_surface_input_critic(llm: Any, github: Any, test_plan: Any,
                                   dev_infos: Iterable[dict | None], *,
                                   exploration: dict | None = None,
                                   protected_titles: frozenset[str] = frozenset()) -> tuple[dict, str | None]:
    """Rule P. Returns ``(changes, gap)``. ``gap`` is set when the critic could
    not run, so the plan says the steps were not checked against the surface."""
    changes: dict = {"performable": [], "rewritten": [], "split": [], "routed_out": [],
                     "marked": [], "unchecked": []}
    items = collect_surface_items(test_plan)
    if not items:
        return changes, None
    prs = [pr for dev in dev_infos for pr in ((dev or {}).get("pull_requests") or [])
           if isinstance(pr, dict) and pr.get("repository")]
    if not prs or not hasattr(llm, "_post_messages"):
        return changes, (f"no linked PRs to search from, so {len(items)} UI case(s) were not checked "
                         "against their surface's inputs (rule P)") if not prs else None
    result = _new_result()
    ws = Workspace(github, prs, max_searches=MAX_SEARCHES, max_reads=MAX_READS)
    gap_tail = f"so {len(items)} UI case(s) were not checked against their surface's inputs (rule P)"
    try:
        raw = await run_tool_loop(
            llm, ws, system=SURFACE_SYSTEM_PROMPT,
            first_message=build_surface_message(items, prs, exploration),
            report_tool=REPORT_SURFACE_TOOL, max_turns=MAX_TURNS, result=result)
    except Exception as e:  # never fails the plan; the gap says what was skipped
        logger.warning("surface input critic failed: %s", e, exc_info=True)
        return changes, f"the surface input critic could not run ({type(e).__name__}), {gap_tail}"
    if raw is None:
        return changes, f"the surface input critic {result['error']}, {gap_tail}"
    changes = apply_surface_verdicts(test_plan, items, parse_surface_verdicts(raw), ws.read_files,
                                     protected_titles=protected_titles, pull_requests=prs)
    changes["usage"] = result["usage"]
    changes["searches"], changes["reads"] = ws.searches, ws.reads
    return changes, None


def summarize(changes: dict) -> str:
    return json.dumps({k: changes.get(k) for k in ("performable", "rewritten", "split", "routed_out",
                                                    "marked", "unchecked", "searches", "reads")})
