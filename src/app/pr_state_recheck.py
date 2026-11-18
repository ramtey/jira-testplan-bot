"""Re-check the PRs a plan is grounded on before it is saved and before it is posted.

A plan records each linked PR's state and head commit when it is generated, and
writes that state into the plan: preconditions ("BLOCKED BY SETUP (rule AF): PR
#31 is not merged or deployed"), the GROUNDED IN lines ("#31 — open — not yet
merged @ c1d2c33"), the DEPLOYMENT banner ("still a draft, not deployed
anywhere") and the build check, which names the merge commits that existed
then. Plans are posted hours or days later. On SK-3084, PR #31 merged 39
minutes after the plan was written and the plan was posted the next day still
saying it was open: two runnable cases were marked blocked, and the build check
named PR #30's merge only, older than the build under test.

So, before save and before every post (the Post button, auto-post, and the post
queued when a plan finishes on a ticket already in testing), every grounding PR
is re-read from GitHub and compared with what the plan recorded:

* **head moved** — new commits since the plan was written: the cases describe
  code that is no longer the PR. Refuse, and say why; regenerating is the fix.
* **closed without merging** — the grounding was abandoned. Refuse.
* **open → merged, same head** — only the label is stale, if the merged tree
  still holds the code the plan read. The files the plan cites are read at the
  grounded head and at the merge commit; identical means the plan is updated
  in place: GROUNDED IN, DEPLOYMENT, build check, and every blocker that rested
  on the PR not being merged. A visible "PR state refreshed" note lists each
  change. Different, or unreadable, refuses: unconfirmed is not current.
* **could not re-read** — the PR's state is unknown now. A blocker that rests
  on it is kept but marked "unverified at post time", never asserted.

Independently of any change, every case blocker that names a PR's merge state
is checked against that PR's current state (the rule-6 assertion), so a
blocker such as "PR #31 is not merged" never survives once #31 has merged.

The edits never add, remove or move a case: section counts, which the progress
key fingerprints, are unchanged.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from .build_targets import _case_paths, path_matches
from .deployed_env_checks import SETUP_MARKER
from .source_grounding import (
    AUTHORITATIVE_STATES,
    PR_STATE_CLOSED_UNMERGED,
    PR_STATE_MERGED,
    PR_STATE_OPEN,
    PR_STATE_UNKNOWN,
)

logger = logging.getLogger(__name__)

PHASE_SAVE = "save"
PHASE_POST = "post"

CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests",
                 "needs_spec_cases")

#: More cited files than this in one PR are not compared file by file; the
#: tree check then reports that it could not confirm the grounding.
MAX_COMPARED_FILES = 40

UNVERIFIED_SUFFIX = " (unverified at post time: {why})"
_UNVERIFIED_MARK_RE = re.compile(r" \(unverified at post time: [^)]*\)")

# "PR #31", "#31", "owner/repo#31", "repo PR #31".
_PR_REF_RE = re.compile(r"(?:\b([\w.-]+/[\w.-]+))?\s*(?:\bPR\s*)?#(\d+)\b", re.I)
# A claim about a PR's merge state — not about deployment alone, which a merge
# does not settle (the build check does).
_NOT_MERGED_RE = re.compile(
    r"\bnot\s+(?:yet\s+)?merged\b|\bunmerged\b|\bisn'?t\s+merged\b|\bhas\s*n[o']t\s+(?:yet\s+)?(?:been\s+)?merged\b"
    r"|\bnot\s+(?:yet\s+)?landed\b|\bhas\s*n[o']t\s+landed\b|\b(?:still\s+)?(?:a\s+)?draft\b"
    r"|\b(?:is\s+)?still\s+open\b|\bopen\s*[—–-]\s*not\s+yet\s+merged\b", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(ts: Any) -> str:
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
    else:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _sha(s: str | None) -> str:
    return (s or "")[:7]


def _same_sha(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


def _label(e: dict) -> str:
    repo, num = e.get("repository"), e.get("number")
    if repo and num:
        return f"{repo}#{num}"
    return f"PR #{num}" if num else (e.get("url") or "a linked PR")


def sha_suffix(entry: dict) -> str:
    """`` @ merge 7e106a0 (head c1d2c33)`` for a merged PR, `` @ c1d2c33`` otherwise.

    The GROUNDED IN line of every renderer (markdown.js mirrors it): a merged
    PR is named by the commit that is on the base branch, which is what a
    build is checked against, not by its branch head.
    """
    head = entry.get("head_sha") or ""
    merge = entry.get("merge_commit_sha") or "" if entry.get("state") == PR_STATE_MERGED else ""
    if len(merge) >= 7:
        out = f" @ merge {merge[:7]}" + (f" (head {head[:7]})" if len(head) >= 7 else "")
    else:
        out = f" @ {head[:7]}" if len(head) >= 7 else ""
    if entry.get("state_at_generation"):
        out += (f" — was {entry['state_at_generation']} when the plan was written; refreshed "
                f"{entry.get('state_refreshed_at') or ''}").rstrip()
    return out


def _provenance(plan: dict) -> dict:
    p = plan.get("source_provenance")
    return p if isinstance(p, dict) else {}


def grounding_entries(plan: dict) -> list[dict]:
    """The provenance rows this plan was grounded on, one per PR URL."""
    out, seen = [], set()
    for e in _provenance(plan).get("pull_requests") or []:
        if not isinstance(e, dict) or not e.get("used_as_grounding") or not e.get("url"):
            continue
        if e["url"] in seen:
            continue
        seen.add(e["url"])
        out.append(e)
    return out


def _iter_cases(plan: dict):
    for section in CASE_SECTIONS:
        for i, case in enumerate(plan.get(section) or []):
            if isinstance(case, dict):
                yield section, i, case


def cited_paths(plan: dict) -> list[str]:
    paths: list[str] = []
    for _, _, case in _iter_cases(plan):
        paths += _case_paths(case)
    return paths


# ---------------------------------------------------------------------------
# Reading GitHub
# ---------------------------------------------------------------------------

async def fetch_current(github, entries: list[dict]) -> dict[str, dict | None]:
    """``{url: {"state", "head_sha", "merge_commit_sha", "merged_at", "files"}}``.

    None for a PR GitHub did not answer about: unread, never "unchanged".
    """
    out: dict[str, dict | None] = {}
    for e in entries:
        url = e["url"]
        try:
            d = await github.fetch_pr_details(url, include_patch=False, include_comments=False)
        except Exception:
            logger.warning("pr_state_recheck: could not re-read %s", url, exc_info=True)
            d = None
        if d is None:
            out[url] = None
            continue
        if getattr(d, "merged", False):
            state = PR_STATE_MERGED
        elif (getattr(d, "state", "") or "").lower() == "open":
            state = PR_STATE_OPEN
        elif (getattr(d, "state", "") or "").lower() == "closed":
            state = PR_STATE_CLOSED_UNMERGED
        else:
            state = PR_STATE_UNKNOWN
        out[url] = {
            "state": state,
            "head_sha": getattr(d, "head_sha", None),
            # GitHub fills merge_commit_sha for open PRs too (a test merge);
            # it only means "the merge" once the PR merged.
            "merge_commit_sha": getattr(d, "merge_commit_sha", None) if state == PR_STATE_MERGED else None,
            "merged_at": getattr(d, "merged_at", None),
            "files": [f.filename for f in getattr(d, "files_changed", None) or []
                      if getattr(f, "filename", None) and getattr(f, "status", "") != "removed"],
        }
    return out


async def merged_tree_matches(github, entry: dict, cur: dict, cited: list[str]) -> tuple[bool | None, dict]:
    """Whether the merge commit holds the same cited files as the grounded head.

    ``(True|False|None, detail)``: None when it could not be compared, which
    the caller treats as not confirmed. Files are the PR's own changed files
    the plan cites, or all of them when the plan cites none of them.
    """
    head, merge, repo = entry.get("head_sha"), cur.get("merge_commit_sha"), entry.get("repository")
    if not head:
        return None, {"why": "the plan did not record which commit it was written from"}
    if not merge or not repo:
        return None, {"why": "GitHub did not give the merge commit"}
    files = cur.get("files") or []
    picked = [f for f in files if any(path_matches(c, f) for c in cited)] or list(files)
    if not picked:
        return True, {"files": []}
    if len(picked) > MAX_COMPARED_FILES:
        return None, {"why": f"{len(picked)} cited files is more than the {MAX_COMPARED_FILES} compared"}
    try:
        at_head = await github.fetch_files_at_ref(repo, head, picked, max_bytes=2_000_000)
        at_merge = await github.fetch_files_at_ref(repo, merge, picked, max_bytes=2_000_000)
    except Exception as exc:
        return None, {"why": f"the files could not be read ({type(exc).__name__})"}
    unread = [f for f in picked if f not in at_head or f not in at_merge]
    if unread:
        return None, {"why": f"could not read {', '.join(unread[:5])} at both commits", "files": picked}
    differ = [f for f in picked if at_head[f] != at_merge[f]]
    if differ:
        return False, {"files": picked, "differ": differ}
    return True, {"files": picked}


# ---------------------------------------------------------------------------
# Classifying what changed
# ---------------------------------------------------------------------------

def classify(entry: dict, cur: dict | None) -> str:
    """``unreadable``, ``head_changed``, ``closed``, ``merged`` or ``unchanged``."""
    if cur is None:
        return "unreadable"
    old_head, new_head = entry.get("head_sha"), cur.get("head_sha")
    if old_head and new_head and not _same_sha(old_head, new_head):
        return "head_changed"
    old, new = entry.get("state"), cur.get("state")
    if new == PR_STATE_CLOSED_UNMERGED and old != PR_STATE_CLOSED_UNMERGED:
        return "closed"
    if new == PR_STATE_MERGED and old != PR_STATE_MERGED:
        return "merged"
    return "unchanged"


# ---------------------------------------------------------------------------
# Applying a verified state change in place
# ---------------------------------------------------------------------------

def _update_entries(plan: dict, url: str, cur: dict, when: str) -> None:
    prov = _provenance(plan)
    rows = list(prov.get("pull_requests") or [])
    for t in (prov.get("by_ticket") or {}).values():
        if isinstance(t, dict):
            rows += t.get("pull_requests") or []
    for e in rows:
        if isinstance(e, dict) and e.get("url") == url and e.get("state") != PR_STATE_MERGED:
            e["state_at_generation"] = e.get("state")
            e["state"] = PR_STATE_MERGED
            e["merged_at"] = cur.get("merged_at")
            e["merge_commit_sha"] = cur.get("merge_commit_sha")
            e["state_refreshed_at"] = when


def _recount(prov: dict) -> None:
    entries = [e for e in prov.get("pull_requests") or [] if isinstance(e, dict)]
    for key, state in (("merged_pr_count", PR_STATE_MERGED), ("open_pr_count", PR_STATE_OPEN),
                       ("excluded_pr_count", PR_STATE_CLOSED_UNMERGED),
                       ("state_unverified_pr_count", PR_STATE_UNKNOWN)):
        if key in prov:
            prov[key] = sum(1 for e in entries if e.get("state") == state)
    grounding = [e for e in entries if e.get("used_as_grounding")]
    prov["grounded_on_unmerged"] = any(e.get("state") in (PR_STATE_OPEN, PR_STATE_UNKNOWN) for e in grounding)
    prov["authoritative"] = bool(grounding) and all(e.get("state") in AUTHORITATIVE_STATES for e in grounding)
    for t in (prov.get("by_ticket") or {}).values():
        if isinstance(t, dict) and t is not prov and t.get("pull_requests") is not None:
            _recount(t)


def _refresh_deployment(plan: dict, merged: list[tuple[dict, dict]], report: dict) -> None:
    ds = plan.get("deployment_status")
    if not isinstance(ds, dict) or not isinstance(ds.get("warnings"), list):
        return
    out, replaced_not_deployed = [], False
    for w in ds["warnings"]:
        hit = None
        if isinstance(w, str):
            for e, cur in merged:
                if _mentions_pr(w, e) and (_NOT_MERGED_RE.search(w) or re.search(r"\bnot\s+deployed\b", w, re.I)):
                    hit = (e, cur)
                    break
        if hit is None:
            out.append(w)
            continue
        e, cur = hit
        new = (f"{_label(e)} merged {_fmt(cur.get('merged_at')) or 'after this plan was written'} "
               f"(merge commit {_sha(cur.get('merge_commit_sha'))}), after this plan was written. The "
               f"earlier note — “{w.strip()}” — no longer holds. Its deployment was not re-read at "
               f"{report['phase']} time: the Build check confirms whether the environment runs it.")
        out.append(new)
        replaced_not_deployed = replaced_not_deployed or w.startswith("Not deployed") or "not deployed" in w.lower()
        report["deployment_notes_replaced"].append({"pr": _label(e), "was": w, "now": new})
    ds["warnings"] = out
    if replaced_not_deployed and ds.get("status") == "not_deployed" and not any(
            isinstance(w, str) and w.startswith("Not deployed") for w in out):
        ds["status"] = "unknown"


def _refresh_build_check(plan: dict, merged: list[tuple[dict, dict]], report: dict) -> None:
    prov = _provenance(plan)
    for e, cur in merged:
        repo, sha = e.get("repository"), cur.get("merge_commit_sha")
        if not sha:
            continue
        for t in prov.get("build_targets") or []:
            if not isinstance(t, dict) or t.get("repo") != repo:
                continue
            if not isinstance(t.get("gate"), dict):
                t["gate"] = {}
            later = t["gate"].setdefault("later", [])
            if not any(isinstance(p, dict) and p.get("number") == e.get("number") for p in later):
                later.append({"number": e.get("number"), "url": e.get("url"), "title": e.get("title"),
                              "sha": sha, "merged": True, "match": None, "refreshed": True})
            t["commit"] = sha
        clause = (f"{_label(e)} merged after this plan was written (merge commit {_sha(sha)}; re-checked at "
                  f"{report['phase']} time): the build must also contain {_sha(sha)} — run `gh api repos/"
                  f"{repo}/compare/{_sha(sha)}...<deployed commit> --jq .status`; `ahead` or `identical` passes.")
        bc = plan.get("build_check")
        if isinstance(bc, dict) and isinstance(bc.get("step"), str) and _sha(sha) not in bc["step"]:
            step = bc["step"]
            stop = " If it does not, stop:"
            bc["step"] = (step.replace(stop, f" {clause}{stop}", 1) if stop in step else f"{step} {clause}")
            if isinstance(bc.get("summary"), str):
                bc["summary"] += f"; and contains {_sha(sha)} ({_label(e)})"
            if repo and isinstance(bc.get("repos"), list) and repo not in bc["repos"]:
                bc["repos"].append(repo)
            bc.setdefault("merge_commits", []).append({"repo": repo, "pr": e.get("number"), "sha": sha})
            report["build_check_added"].append({"pr": _label(e), "sha": sha})
        facts = plan.get("build_and_ci")
        if isinstance(facts, list):
            facts.append({"kind": "build_gate", "repo": repo, "evidence": "",
                          "finding": f"{repo}: {_label(e)} merged after this plan was written (merge commit "
                                     f"{_sha(sha)}); the build under test must contain it too."})


def _mentions_pr(text: str, e: dict) -> bool:
    num = e.get("number")
    if not num:
        return False
    for m in _PR_REF_RE.finditer(text):
        if int(m.group(2)) != num:
            continue
        repo = m.group(1)
        if repo and e.get("repository") and repo.lower() != e["repository"].lower():
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Rule 6: a blocker that names a PR's merge state must match it now
# ---------------------------------------------------------------------------

# A clause that only says who can unblock the one before it ("owner <name>") belongs to that clause: dropping the blocker and keeping its owner
# would leave "BLOCKED BY SETUP: owner <name>", a blocker with no reason.
_ATTRIBUTION_RE = re.compile(r"^(?:owner|owned\s+by|ask|contact|who|cc|via)\b", re.I)


def _clauses(blocked: str) -> list[str]:
    out: list[str] = []
    for c in (c.strip() for c in re.split(r";\s+", blocked)):
        if not c:
            continue
        if out and _ATTRIBUTION_RE.match(c):
            out[-1] = f"{out[-1]}; {c}"
        else:
            out.append(c)
    return out


STALE_SENTENCE_MARK = " [stale: {pr} has since merged as {sha} — re-checked at {phase} time]"


def mark_stale_state_sentences(plan: dict, states: dict[str, dict], *, phase: str, report: dict) -> bool:
    """Mark prose that still calls a merged PR a draft or unmerged.

    Blockers are structured (``blocked_by_setup``); the model also writes the
    same claim as prose in a case's preconditions or environment, or in a
    shared setup step ("Blocked until draft PR #31 is deployed"). That prose is
    marked where it stands, never deleted: the sentence may carry a deployment
    requirement that a merge does not settle.
    """
    merged = [(e, states.get(e["url"]) or {}) for e in grounding_entries(plan)]
    merged = [(e, s) for e, s in merged if s.get("readable") and s.get("state") == PR_STATE_MERGED]
    if not merged:
        return False

    def mark(text: str) -> str:
        parts = re.split(r"(?<=[.!?])\s+", text)
        out, hit_any = [], False
        for sent in parts:
            if "[stale:" not in sent and _NOT_MERGED_RE.search(sent) and "(rule AF)" not in sent:
                hit = next(((e, s) for e, s in merged if _mentions_pr(sent, e)), None)
                if hit:
                    e, st = hit
                    sent = sent + STALE_SENTENCE_MARK.format(pr=_label(e), sha=_sha(st.get("merge_commit_sha"))
                                                            or "a merge commit", phase=phase)
                    hit_any = True
            out.append(sent)
        return " ".join(out) if hit_any else text

    changed = False
    for section, i, case in _iter_cases(plan):
        for key in ("preconditions", "environment"):
            v = case.get(key)
            if isinstance(v, str):
                new = mark(v)
                if new != v:
                    case[key] = new
                    changed = True
                    report["stale_sentences_marked"].append({"case": f"{section}:{i}", "field": key})
    for sp in plan.get("shared_preconditions") or []:
        if isinstance(sp, dict) and isinstance(sp.get("text"), str):
            new = mark(sp["text"])
            if new != sp["text"]:
                sp["text"] = new
                changed = True
                report["stale_sentences_marked"].append({"case": sp.get("id") or "shared", "field": "text"})
    return changed


def check_pr_blockers(plan: dict, states: dict[str, dict], *, phase: str, report: dict) -> bool:
    """Drop or mark every case blocker whose reason is a PR's merge state.

    ``states`` maps each grounding PR's URL to ``{"state", "merge_commit_sha",
    "merged_at", "readable"}`` as known now. A clause that names a PR now
    merged is dropped (with a note in the case); one whose PR could not be
    re-read, or is not one this plan recorded, is marked unverified — at post
    time only, since at save time the plan has just read them. Returns whether
    anything changed.
    """
    entries = grounding_entries(plan)
    changed = False
    for section, i, case in _iter_cases(plan):
        blocked = case.get("blocked_by_setup")
        if not isinstance(blocked, str) or not blocked.strip():
            continue
        keep, notes = [], []
        for clause in _clauses(blocked):
            if not _NOT_MERGED_RE.search(clause) or not _PR_REF_RE.search(clause):
                keep.append(clause)
                continue
            # An earlier post that could not re-read the PR marked this clause.
            # Judge it afresh: the mark must go once the state is known.
            clause = _UNVERIFIED_MARK_RE.sub("", clause)
            named = [e for e in entries if _mentions_pr(clause, e)]
            st = [states.get(e["url"]) or {} for e in named]
            if named and all(s.get("readable") and s.get("state") == PR_STATE_MERGED for s in st):
                for e, s in zip(named, st):
                    notes.append(f"{_label(e)} merged {_fmt(s.get('merged_at')) or ''} (merge commit "
                                 f"{_sha(s.get('merge_commit_sha'))})".replace("  ", " "))
                report["blockers_dropped"].append({"case": f"{section}:{i}", "title": case.get("title"),
                                                   "blocker": clause})
                continue
            if named and all(s.get("readable") for s in st):
                keep.append(clause)  # still open: the blocker holds
                continue
            if phase != PHASE_POST:
                keep.append(clause)
                continue
            why = ("its state could not be re-read" if named else
                   "it is not a PR this plan recorded, so its state was not re-read")
            keep.append(clause + UNVERIFIED_SUFFIX.format(why=why))
            report["blockers_unverified"].append({"case": f"{section}:{i}", "title": case.get("title"),
                                                  "blocker": clause, "why": why})
        new_blocked = "; ".join(keep)
        if new_blocked == blocked.strip():
            continue
        changed = True
        pre = case.get("preconditions") if isinstance(case.get("preconditions"), str) else ""
        old_marker = SETUP_MARKER.format(what=blocked)
        new_marker = SETUP_MARKER.format(what=new_blocked) if new_blocked else ""
        if old_marker in pre:
            pre = pre.replace(old_marker, new_marker, 1)
        else:
            for clause in _clauses(blocked):
                if clause not in keep and clause in pre:
                    pre = pre.replace(clause, "", 1)
        if notes:
            pre = (f"PR state re-checked at {phase} time: {'; '.join(notes)}, so the blocker that waited for "
                   f"that merge no longer applies. " + pre)
        case["preconditions"] = re.sub(r"\s{2,}", " ", pre).strip()
        if new_blocked:
            case["blocked_by_setup"] = new_blocked
        else:
            case.pop("blocked_by_setup", None)
            case["blocked_by_setup_cleared"] = True
        for item in case.get("required_setup") or []:
            if (isinstance(item, dict) and isinstance(item.get("need"), str)
                    and _NOT_MERGED_RE.search(item["need"]) and item["need"].strip() not in new_blocked
                    and any(_mentions_pr(item["need"], e) for e in entries)):
                item["status"] = "exists"
                item["rechecked"] = f"merged; re-checked at {phase} time"
    return changed


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

#: What a refresh did to the plan stays listed on every later post; what one
#: post could not confirm is replaced by the next post's reading.
_DURABLE = ("changes", "blockers_dropped", "deployment_notes_replaced", "build_check_added",
            "stale_sentences_marked")
_TRANSIENT = ("blockers_unverified", "unreadable", "refuse")


def _write_note(plan: dict, report: dict) -> None:
    """Set ``plan["pr_state_refresh"]``, the visible note, from this pass and the last."""
    prev = plan.get("pr_state_refresh") if isinstance(plan.get("pr_state_refresh"), dict) else None
    shown_unreadable = report["unreadable"] if report["phase"] == PHASE_POST else []
    if not (any(report[k] for k in _DURABLE) or report["blockers_unverified"] or shown_unreadable or prev):
        return
    note = {k: v for k, v in report.items() if k not in ("modified",) + _DURABLE + _TRANSIENT}
    for k in _DURABLE:
        note[k] = list((prev or {}).get(k) or []) + list(report[k])
    note["blockers_unverified"] = list(report["blockers_unverified"])
    note["unreadable"] = list(shown_unreadable)
    note["refuse"] = []
    if prev and prev.get("plan_created_at") and not note.get("plan_created_at"):
        note["plan_created_at"] = prev["plan_created_at"]
    if prev is not None and all(prev.get(k) == note.get(k) for k in _DURABLE + _TRANSIENT[:2]):
        return  # nothing new to say; leave the note (and its time) as it was
    plan["pr_state_refresh"] = note
    report["modified"] = True


def _empty_report(phase: str, when: str, created_at: Any) -> dict:
    return {
        "phase": phase, "checked_at": when, "plan_created_at": _fmt(created_at) or None,
        "checked": 0, "changes": [], "unreadable": [], "refuse": [],
        "blockers_dropped": [], "blockers_unverified": [], "deployment_notes_replaced": [],
        "build_check_added": [], "stale_sentences_marked": [], "unmerged_flags_cleared": 0,
        "modified": False,
    }


async def recheck_pr_states(plan: dict, *, phase: str, github=None, created_at: Any = None,
                            now: datetime | None = None) -> dict:
    """Re-read every grounding PR and bring ``plan`` in line, in place. Never raises.

    Returns the report. ``report["refuse"]`` non-empty means the plan's
    grounding is out of date and it must not be posted as it is; the plan is
    then left untouched. ``report["modified"]`` says whether the plan changed.
    """
    when = _fmt(now or _now())
    report = _empty_report(phase, when, created_at or plan.get("created_at"))
    entries = grounding_entries(plan)
    report["checked"] = len(entries)
    if not entries:
        return report
    if github is None:
        from .github_client import GitHubClient

        github = GitHubClient()
    try:
        current = await fetch_current(github, entries)
    except Exception as exc:  # fetch_current catches per PR; this is a bug guard
        logger.exception("pr_state_recheck: re-read failed")
        current = {e["url"]: None for e in entries}
        report["error"] = f"{type(exc).__name__}: {exc}"

    cited = cited_paths(plan)
    merged: list[tuple[dict, dict]] = []
    states: dict[str, dict] = {}
    for e in entries:
        cur = current.get(e["url"])
        kind = classify(e, cur)
        label = _label(e)
        if kind == "unreadable":
            report["unreadable"].append(label)
            # At save time the plan has just read it; its recorded state stands.
            states[e["url"]] = {"state": e.get("state"), "merge_commit_sha": e.get("merge_commit_sha"),
                                "merged_at": e.get("merged_at"), "readable": phase != PHASE_POST}
            continue
        states[e["url"]] = {**cur, "readable": True}
        if kind == "head_changed":
            report["refuse"].append(
                f"{label} has new commits since the plan was written (grounded on {_sha(e.get('head_sha'))}, "
                f"now {_sha(cur.get('head_sha'))}), so its cases describe code that is no longer the PR — "
                f"regenerate the plan")
        elif kind == "closed":
            report["refuse"].append(f"{label} was closed without merging after the plan was written, so the "
                                    f"code its cases were written from was abandoned — regenerate the plan")
        elif kind == "merged":
            ok, detail = await merged_tree_matches(github, e, cur, cited)
            change = {"pr": label, "url": e["url"], "from": e.get("state"), "to": PR_STATE_MERGED,
                      "merged_at": cur.get("merged_at"), "merge_commit_sha": cur.get("merge_commit_sha"),
                      "head_sha": e.get("head_sha"), "files_compared": detail.get("files") or []}
            if ok is True:
                merged.append((e, cur))
                report["changes"].append(change)
            elif ok is False:
                report["refuse"].append(
                    f"{label} merged after the plan was written, and the merged code differs from what the plan "
                    f"was written from in {', '.join(detail['differ'][:5])} — regenerate the plan")
            else:
                report["refuse"].append(
                    f"{label} merged after the plan was written, and the merged code could not be confirmed "
                    f"identical to what the plan was written from ({detail.get('why')}) — regenerate the plan")
    if report["refuse"]:
        return report

    if merged:
        for e, cur in merged:
            _update_entries(plan, e["url"], cur, when)
        prov = _provenance(plan)
        _recount(prov)
        if not prov.get("grounded_on_unmerged"):
            for _, _, case in _iter_cases(plan):
                if case.get("grounded_in_unmerged"):
                    case["grounded_in_unmerged"] = False
                    case["grounded_in_unmerged_cleared"] = True
                    report["unmerged_flags_cleared"] += 1
        _refresh_deployment(plan, merged, report)
        _refresh_build_check(plan, merged, report)
        report["modified"] = True
    if check_pr_blockers(plan, states, phase=phase, report=report):
        report["modified"] = True
    if mark_stale_state_sentences(plan, states, phase=phase, report=report):
        report["modified"] = True

    _write_note(plan, report)
    prov = plan.get("source_provenance")
    if isinstance(prov, dict):
        prov.setdefault("pr_state_checks", []).append(
            {k: report[k] for k in ("phase", "checked_at", "checked", "unreadable")}
            | {"changes": len(report["changes"]), "blockers_dropped": len(report["blockers_dropped"]),
               "blockers_unverified": len(report["blockers_unverified"])})
    return report
