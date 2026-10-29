"""Runs of a ticket's live plan by an unattended runner, and the results they record.

A run is pinned to the plan that is live in the tracker when it starts, split
into legs (one per runner profile its cases need), and closed by `finalize`,
which gives every case a result or a reason. The runner itself reaches this
module only through the HTTP routes in ``uat_routes`` — via the MCP server in
``src/uat_mcp`` — and never writes to the tracker.

Runner profiles are local configuration (``UAT_RUNNER_PROFILES_FILE``), keyed
by profile name::

    {"mobile": {"repos": ["acme/mobile-app"], "surfaces": ["mobile"],
                "agent": "uat-runner", "cwd": "/abs/path/to/mobile-app",
                "settings": "/abs/path/to/allowlist.json",
                "context": {"simulator_udid": "..."},
                "http_allowed_hosts": ["api.integ.example.com"]}}

A case's ``surface`` decides which profile runs it; the repos it exercises
(``build_targets.targets_for_case``) decide whether it can run yet.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..build_targets import build_targets, targets_for_case
from ..config import settings
from ..db.base import utcnow
from ..db.models.case_result import CaseResult
from ..db.models.uat_run import NOT_RUN_CODES, UatRun
from ..jira_client import JiraClient
from ..repositories import case_result_repository, plan_repository, uat_run_repository
from . import progress_key as progress_key_service

logger = logging.getLogger(__name__)

EVIDENCE_KINDS = frozenset({"assertion", "screenshot", "video_ts", "unit_test", "http"})


class RunRefused(Exception):
    """A run cannot start, or a write cannot be accepted. ``code`` is stable."""

    def __init__(self, code: str, message: str, status: int = 409, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------
# Runner profiles
# ---------------------------------------------------------------------------


def load_profiles(path: str | None = None) -> dict[str, dict]:
    """The runner profiles, or {} when none are configured.

    A file that exists but cannot be read is an error, not "no profiles":
    every case would otherwise be reported ``no_runner_profile`` for a typo.
    """
    raw = path if path is not None else settings.uat_runner_profiles_file
    if not raw:
        return {}
    p = Path(os.path.expanduser(raw))
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise RunRefused("profiles_unreadable", f"{p} could not be read: {e}", status=500)
    if not isinstance(data, dict):
        raise RunRefused("profiles_unreadable", f"{p} must hold a JSON object", status=500)
    return {name: prof for name, prof in data.items() if isinstance(prof, dict)}


def _profile_for(case: dict, repos: list[str], profiles: dict[str, dict]) -> str | None:
    """The profile that runs `case`: it covers the case's surface and, when the
    case exercises repos, the first of them (the one its surface runs on)."""
    surface = case.get("surface") or ""
    if not surface:
        # Regression items are bare strings with no surface: whoever covers the
        # repo runs them, when exactly one profile does.
        covering = [n for n, p in profiles.items() if repos and repos[0] in (p.get("repos") or [])]
        return covering[0] if len(covering) == 1 else None
    on_surface = [n for n, p in profiles.items() if surface in (p.get("surfaces") or [])]
    if repos:
        for name in on_surface:
            if repos[0] in (profiles[name].get("repos") or []):
                return name
        for name in on_surface:
            if set(repos) & set(profiles[name].get("repos") or []):
                return name
        return None
    # No repo to go by (a plan written from the spec alone): only an
    # unambiguous surface match is used, never a guess between two.
    return on_surface[0] if len(on_surface) == 1 else None


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def _build_for(target: dict, prs: list[dict]) -> dict:
    """Whether `target`'s change is in the build the runner will test.

    One of: included, not_included, unverifiable (a source exists and could not
    be read), unverified (no source configured: the runner's UI gate is the only
    check), pr_not_merged.
    """
    repo = target["repo"]
    repo_prs = [p for p in prs if p.get("repository") == repo]
    open_prs = [p for p in repo_prs if (p.get("status") or "").upper() == "OPEN"]
    out = {"repo": repo, "merge_commit": target.get("commit"), "platform": target.get("platform"),
           "pr_numbers": [p.get("number") for p in repo_prs if p.get("number")],
           "status": "unverified", "source": "unverified", "env": None,
           "deployed_commit": None, "detail": None, "checked_at": utcnow().isoformat()}
    if open_prs:
        nums = ", ".join(f"#{p.get('number')}" for p in open_prs)
        return {**out, "status": "pr_not_merged", "detail": f"{repo} {nums} not merged"}
    deploys = target.get("deploy") or []
    if not deploys:
        return {**out, "detail": "no deploy-state source configured; the UI gate is the only check"}
    # Repos name the same environment differently (integ, integ-west), so the
    # setting is an ordered list and each repo uses the first name it has.
    wanted = [e.strip() for e in (settings.uat_test_env or "").split(",") if e.strip()]
    have = {d.get("env") for d in deploys}
    env = next((e for e in wanted if e in have), None)
    chosen = [d for d in deploys if d.get("env") == env] if wanted else deploys
    if not chosen:
        envs = ", ".join(sorted(str(e) for e in have))
        return {**out, "status": "unverifiable", "source": "deploy_state",
                "detail": f"{repo} has no source for UAT_TEST_ENV={settings.uat_test_env} (has: {envs})"}
    if len(chosen) > 1:
        envs = ", ".join(sorted(str(d.get("env")) for d in chosen))
        return {**out, "status": "unverifiable", "source": "deploy_state",
                "detail": f"UAT_TEST_ENV is not set and {repo} deploys to {envs}"}
    d = chosen[0]
    out.update(source="deploy_state", env=d.get("env"), deployed_commit=d.get("deployed_commit"))
    if d.get("error") or d.get("included") is None:
        return {**out, "status": "unverifiable", "detail": d.get("error") or "comparison failed"}
    if d.get("included"):
        return {**out, "status": "included"}
    return {**out, "status": "not_included",
            "detail": f"{d.get('env')} runs {d.get('tag') or d.get('deployed_commit')}, "
                      f"which does not contain {(out['merge_commit'] or '')[:7]}"}


async def read_ticket_builds(ticket_key: str, *, jira: JiraClient | None = None) -> dict:
    """The ticket's PRs, build targets and per-repo build answer, read now.

    Raises the Jira error family when the ticket cannot be read: without its PRs
    no case can be routed, so the caller refuses the run rather than guessing.
    """
    from . import plan_service

    issue = await (jira or JiraClient()).get_issue(ticket_key)
    dev = plan_service.development_info_to_dict(issue.development_info) or {}
    prs = [p for p in dev.get("pull_requests") or [] if isinstance(p, dict)]
    prs = [p for p in prs if (p.get("status") or "").upper() != "DECLINED"]
    deployments = await plan_service._discover_deployments(dev)
    deploy_state = await plan_service._read_deploy_state(dev, deployments)
    targets = build_targets(prs, None, deploy_state,
                            deployments.get("deployments_repos") or settings.deployments_repos)
    return {
        "issue_type": issue.issue_type,
        "status": issue.status,
        "dev_status_unavailable": bool(getattr(issue, "dev_status_unavailable", False)),
        "targets": targets,
        "builds": [_build_for(t, prs) for t in targets],
        "pr_count": len(prs),
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_cases(cases: list[dict], targets: list[dict], builds: list[dict],
                profiles: dict[str, dict]) -> dict[str, dict]:
    """``{case_id: {"leg", "repos", "not_run"}}`` for every non-optional case.

    ``leg`` is None exactly when ``not_run`` says why the case will not run in
    this run. Optional cases (already unit-tested) are left out: they are not
    part of the checklist a run is reviewed against.
    """
    build_by_repo = {b["repo"]: b for b in builds}
    routing: dict[str, dict] = {}
    for c in cases:
        if c.get("optional"):
            continue
        case = c.get("case") if isinstance(c.get("case"), dict) else {"title": c.get("case")}
        repos = [t["repo"] for t in targets_for_case(case, targets)]
        entry = {"leg": None, "repos": repos, "not_run": None,
                 "surface": case.get("surface"), "title": c.get("title")}
        blocked = None
        for repo in repos:
            b = build_by_repo.get(repo) or {}
            if b.get("status") == "pr_not_merged":
                blocked = {"code": "pr_not_merged", "detail": b.get("detail")}
            elif b.get("status") == "not_included":
                blocked = blocked or {"code": "waiting_for_build", "detail": f"{repo}: {b.get('detail')}"}
            elif b.get("status") == "unverifiable":
                blocked = blocked or {"code": "blocked_environment",
                                      "detail": f"build unverifiable for {repo}: {b.get('detail')}"}
        leg = _profile_for(case, repos, profiles)
        if case.get("surface") == "manual_only":
            entry["not_run"] = {"code": "not_automatable", "detail": "the plan marks this case manual only"}
        elif blocked:
            entry["not_run"] = blocked
        elif leg is None:
            what = ", ".join(repos) if repos else f"surface {case.get('surface') or 'unknown'}"
            entry["not_run"] = {"code": "no_runner_profile", "detail": f"no runner profile covers {what}"}
        else:
            entry["leg"] = leg
        routing[c["id"]] = entry
    return routing


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


async def _live_own_plan(db, ticket_key: str):
    live = await plan_repository.list_live_plans_for_ticket(db, ticket_key=ticket_key)
    return next(((r, p) for r, p in live if len(r.ticket_keys or []) == 1), None)


async def start_run(db: AsyncIOMotorDatabase, *, ticket_key: str, trigger: str = "command",
                    dry_run: bool = False, budget_usd: float | None = None,
                    jira: JiraClient | None = None, profiles: dict | None = None) -> UatRun:
    """Pin a run to the ticket's live plan and route every case.

    Refuses (never queues silently) when the plan is not posted, when another
    run is active, or when the ticket cannot be read.
    """
    key = ticket_key.upper()
    found = await _live_own_plan(db, key)
    if found is None:
        raise RunRefused("waiting_for_plan_post",
                         f"{key} has no plan of its own live in the tracker; post one first")
    run_row, plan = found
    existing = await uat_run_repository.active_for_ticket(db, ticket_key=key)
    if existing is not None:
        raise RunRefused("run_active", f"run {existing.id} is already active for {key}",
                         run_id=existing.id)
    profiles = load_profiles() if profiles is None else profiles
    try:
        ticket = await read_ticket_builds(key, jira=jira)
    except Exception as e:
        raise RunRefused("ticket_unreadable",
                         f"{key} could not be read ({type(e).__name__}: {e}); no case was routed",
                         status=503)
    if ticket["dev_status_unavailable"]:
        raise RunRefused("ticket_unreadable",
                         f"the tracker did not return {key}'s pull requests; builds cannot be checked",
                         status=503)

    cases = progress_key_service.cases_by_id(plan.body)
    routing = route_cases(cases, ticket["targets"], ticket["builds"], profiles)
    legs = {}
    for name in sorted({r["leg"] for r in routing.values() if r["leg"]}):
        prof = profiles[name]
        legs[name] = {"repos": prof.get("repos") or [], "surfaces": prof.get("surfaces") or [],
                      "agent": prof.get("agent"), "cwd": prof.get("cwd"), "status": "pending",
                      "case_ids": [c for c, r in routing.items() if r["leg"] == name],
                      "cost_usd": 0.0, "permission_denials": []}
    run = UatRun(
        ticket_key=key, plan_id=plan.id, plan_version=plan.version,
        progress_key=progress_key_service.build_progress_key(list(run_row.ticket_keys or []), plan.body),
        issue_type=ticket["issue_type"], trigger=trigger, dry_run=dry_run,
        budget_usd=budget_usd, legs=legs, builds=ticket["builds"], routing=routing,
        ticket_snapshot={"status": ticket["status"], "pr_count": ticket["pr_count"]},
    )
    if not legs:
        run.status = "awaiting_review"
        run.status_reason = "no case can run now"
    try:
        run = await uat_run_repository.create(db, run)
    except uat_run_repository.ActiveRunExists as e:
        raise RunRefused("run_active", f"run {e.run_id} is already active for {key}", run_id=e.run_id)
    run.evidence_dir = str(Path(os.path.expanduser(settings.uat_evidence_root)) / key / f"run-{run.id}")
    await uat_run_repository.save(db, run)
    # Cases that will not run in this run get their reason now, so the review
    # shows them from the start rather than as gaps.
    for case_id, r in routing.items():
        if r["not_run"]:
            await _record(db, run, case_id=case_id, status="not_run", not_run=r["not_run"],
                          actor={"kind": "agent", "name": "router"}, source="router")
    if not legs:
        await _close(db, run)
    return run


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


async def _record(db, run: UatRun, *, case_id: str, status: str, actor: dict, source: str,
                  **extra: Any) -> CaseResult:
    return await case_result_repository.record(
        db, progress_key=run.progress_key, plan_id=run.plan_id, case_id=case_id,
        status=status, actor=actor, source=source, run_id=run.id,
        case_title=(run.routing.get(case_id) or {}).get("title"), **extra,
    )


def _check_evidence(evidence: Any) -> list[dict]:
    if not isinstance(evidence, list):
        return []
    return [e for e in evidence if isinstance(e, dict) and e.get("kind") in EVIDENCE_KINDS
            and (e.get("ref") or e.get("text"))]


async def record_result(db: AsyncIOMotorDatabase, run: UatRun, *, leg: str, payload: dict) -> dict:
    """Record one runner result on `run`. Idempotent on ``client_id``.

    Refused: a case outside the run or routed to another leg, a pass or failure
    with no evidence, a Not Run without a known code and a detail, any write to
    a run a person has already decided.
    """
    client_id = payload.get("client_id")
    if client_id:
        dup = await uat_run_repository.result_by_client_id(db, client_id)
        if dup is not None:
            return {"recorded": False, "duplicate": True, "result_id": dup.id}
    if run.status in ("approved", "rejected"):
        raise RunRefused("run_closed", f"run {run.id} was {run.status}; nothing more is recorded")
    if leg not in run.legs:
        raise RunRefused("unknown_leg", f"run {run.id} has no leg {leg!r}", status=422)
    case_id = payload.get("case_id")
    route = run.routing.get(case_id)
    if route is None:
        raise RunRefused("unknown_case_id", f"{case_id!r} names no case in this run", status=422,
                         known=sorted(run.legs[leg].get("case_ids") or []))
    if route.get("leg") != leg:
        where = route.get("leg") or f"no leg ({(route.get('not_run') or {}).get('code')})"
        raise RunRefused("wrong_leg", f"{case_id} is routed to {where}, not {leg}", status=422)
    status = payload.get("status")
    if status not in ("passed", "failed", "not_run"):
        raise RunRefused("bad_status", "status must be passed, failed or not_run", status=422)
    evidence = _check_evidence(payload.get("evidence"))
    not_run = None
    if status in ("passed", "failed") and not evidence:
        raise RunRefused("evidence_required",
                         f"a {status} result needs evidence: at least one item with kind "
                         f"({', '.join(sorted(EVIDENCE_KINDS))}) and a ref or text", status=422)
    if status == "not_run":
        nr = payload.get("not_run") or {}
        if nr.get("code") not in NOT_RUN_CODES or not (nr.get("detail") or "").strip():
            raise RunRefused("reason_required",
                             "a not_run result needs not_run.code (one of "
                             f"{', '.join(sorted(NOT_RUN_CODES))}) and a non-empty not_run.detail",
                             status=422)
        not_run = {"code": nr["code"], "detail": nr["detail"].strip()}
    actor = {"kind": "agent", "name": run.legs[leg].get("agent"), "leg": leg, "run_id": run.id}
    try:
        row = await _record(
            db, run, case_id=case_id, status=status, actor=actor, source="runner",
            not_run=not_run, evidence=evidence, leg=leg, client_id=client_id,
            flaky=bool(payload.get("flaky")), attempts=int(payload.get("attempts") or 1),
            started_at=payload.get("started_at"), finished_at=payload.get("finished_at"),
        )
    except Exception as e:
        # The unique index on client_id: a replay raced the original write.
        if client_id and type(e).__name__ == "DuplicateKeyError":
            dup = await uat_run_repository.result_by_client_id(db, client_id)
            return {"recorded": False, "duplicate": True, "result_id": dup.id if dup else None}
        raise
    return {"recorded": True, "duplicate": False, "result_id": row.id}


def latest_by_case(results: list[CaseResult]) -> dict[str, CaseResult]:
    out: dict[str, CaseResult] = {}
    for r in results:
        out[r.case_id] = r
    return out


def counts(results: list[CaseResult]) -> dict[str, int]:
    latest = latest_by_case(results)
    out = {"passed": 0, "failed": 0, "not_run": 0}
    for r in latest.values():
        if r.status in out:
            out[r.status] += 1
    return out


# ---------------------------------------------------------------------------
# Reading a run
# ---------------------------------------------------------------------------


def _result_view(r: CaseResult) -> dict:
    return {"case_id": r.case_id, "status": r.status, "not_run": r.not_run,
            "evidence": r.evidence, "leg": r.leg, "source": r.source, "flaky": r.flaky,
            "recorded_at": r.created_at.isoformat() if r.created_at else None}


async def run_view(db: AsyncIOMotorDatabase, run: UatRun, *, leg: str | None = None) -> dict:
    """The run as its runner (one leg) or a reviewer (no leg) needs it.

    For a leg: its cases in full, each with the builds of the repos it
    exercises and any result already recorded — a rerun skips those.
    """
    from .. import video_walkthrough

    found = await plan_repository.get_plan_with_cases(db, plan_id=run.plan_id)
    body = None
    if found is not None:
        try:
            body = json.loads(found[0].body) if found[0].body else None
        except (ValueError, TypeError):
            body = None
    cases = {c["id"]: c for c in progress_key_service.cases_by_id(body)}
    results = latest_by_case(await uat_run_repository.results_for_run(db, run.id))
    builds = {b["repo"]: b for b in run.builds}
    ids = run.legs[leg]["case_ids"] if leg else list(run.routing)
    view_cases = []
    for cid in ids:
        route = run.routing.get(cid) or {}
        c = cases.get(cid) or {}
        view_cases.append({
            "id": cid, "section": c.get("section"), "title": c.get("title") or route.get("title"),
            "surface": route.get("surface"), "leg": route.get("leg"),
            "repos": route.get("repos") or [],
            "builds": [builds[r] for r in route.get("repos") or [] if r in builds],
            "case": c.get("case"),
            "routed_not_run": route.get("not_run"),
            "result": _result_view(results[cid]) if cid in results else None,
        })
    out = {
        "run_id": run.id, "ticket_key": run.ticket_key, "plan_id": run.plan_id,
        "plan_version": run.plan_version, "progress_key": run.progress_key,
        "status": run.status, "status_reason": run.status_reason, "dry_run": run.dry_run,
        "evidence_dir": run.evidence_dir, "issue_type": run.issue_type,
        "builds": run.builds, "legs": run.legs, "cases": view_cases,
        "counts": counts(list(results.values())),
    }
    if leg:
        out["leg"] = leg
        out["how_to_see_it"] = (body or {}).get("how_to_see_it") if isinstance(body, dict) else None
        out["video_walkthrough"] = video_walkthrough.walkthrough_from_plan_body(body)
        # Results this leg still owes. finish_run refuses to call it done while
        # any remain; finalize fills them as run_aborted.
        out["missing"] = [cid for cid in ids if cid not in results]
    return out


# ---------------------------------------------------------------------------
# Continue / cancel / finish
# ---------------------------------------------------------------------------


async def should_continue(db: AsyncIOMotorDatabase, run: UatRun, *,
                          jira: JiraClient | None = None) -> dict:
    """Whether the runner should start its next case.

    Stops when the run was cancelled, when the pinned plan is no longer the
    live one, or when the ticket moved out of the status it had at start. A
    check that could not be made is reported as unchecked, never as passed.
    """
    if run.status != "running":
        return {"continue": False, "reason": run.status_reason or run.status, "checks": {}}
    checks: dict[str, Any] = {}
    try:
        found = await _live_own_plan(db, run.ticket_key)
        live_id = found[1].id if found else None
        checks["plan_live"] = live_id == run.plan_id
        if live_id != run.plan_id:
            what = f"superseded by plan {live_id} v{found[1].version}" if found else "no plan is live"
            await cancel(db, run, reason=f"stale_plan: {what}", code="plan_changed")
            return {"continue": False, "reason": f"stale_plan: {what}", "checks": checks}
    except Exception as e:
        checks["plan_live"] = None
        checks["plan_live_error"] = f"{type(e).__name__}: {e}"
    started = (run.ticket_snapshot or {}).get("status")
    try:
        now = await (jira or JiraClient()).get_issue_status(run.ticket_key)
        checks["ticket_status"] = now
        if started and now and now != started:
            await cancel(db, run, reason=f"ticket moved to {now}")
            return {"continue": False, "reason": f"ticket moved to {now}", "checks": checks}
    except Exception as e:
        checks["ticket_status"] = None
        checks["ticket_status_error"] = f"{type(e).__name__}: {e}"
    return {"continue": True, "reason": None, "checks": checks}


async def cancel(db: AsyncIOMotorDatabase, run: UatRun, *, reason: str,
                 code: str = "run_cancelled") -> UatRun:
    """Stop `run`. Cases without a result are filled when each leg finalizes."""
    if run.status != "running":
        return run
    run.status = "cancelled"
    run.status_reason = reason
    run.status_detail = code
    await uat_run_repository.save(db, run)
    return run


async def start_leg(db: AsyncIOMotorDatabase, run: UatRun, leg: str, info: dict) -> None:
    if leg not in run.legs:
        raise RunRefused("unknown_leg", f"run {run.id} has no leg {leg!r}", status=422)
    await uat_run_repository.update_leg(db, run.id, leg, {
        "status": "running", "started_at": utcnow().isoformat(),
        **{k: info.get(k) for k in ("pid", "host", "model", "session_id") if info.get(k) is not None},
    })


async def finalize_leg(db: AsyncIOMotorDatabase, run: UatRun, leg: str, info: dict) -> UatRun:
    """Close one leg: record what it cost and what was refused, and give every
    case it owed a result. When the last leg closes the run awaits review.

    Idempotent: a leg already closed is returned unchanged.
    """
    if leg not in run.legs:
        raise RunRefused("unknown_leg", f"run {run.id} has no leg {leg!r}", status=422)
    if run.legs[leg].get("status") in ("finished", "failed"):
        return run
    exit_code = info.get("exit_code")
    detail = info.get("detail")
    results = latest_by_case(await uat_run_repository.results_for_run(db, run.id))
    if run.status == "cancelled":
        code, why = run.status_detail or "run_cancelled", run.status_reason or "run cancelled"
    elif info.get("missing_code") in NOT_RUN_CODES:
        code, why = info["missing_code"], detail or info["missing_code"]
    elif exit_code not in (0, None):
        code, why = "run_aborted", f"runner exited {exit_code}" + (f": {detail}" if detail else "")
    else:
        code, why = "run_aborted", detail or "runner finished without recording a result"
    for cid in run.legs[leg]["case_ids"]:
        if cid not in results:
            await _record(db, run, case_id=cid, status="not_run",
                          not_run={"code": code if code in NOT_RUN_CODES else "run_aborted", "detail": why},
                          actor={"kind": "agent", "name": "finalize", "leg": leg}, source="finalize",
                          leg=leg)
    fields = {
        "status": "finished" if exit_code in (0, None) else "failed",
        "finished_at": utcnow().isoformat(), "exit_code": exit_code, "detail": detail,
        "cost_usd": float(info.get("cost_usd") or 0.0),
        "permission_denials": info.get("permission_denials") or [],
        "session_id": info.get("session_id"), "duration_s": info.get("duration_s"),
        "budget_alert": info.get("budget_alert"),
    }
    await uat_run_repository.update_leg(db, run.id, leg, fields)
    run = await uat_run_repository.get(db, run.id)
    if all(l.get("status") in ("finished", "failed") for l in run.legs.values()):
        await _close(db, run)
    return run


async def _close(db, run: UatRun) -> None:
    run.cost_usd = round(sum(float(l.get("cost_usd") or 0) for l in run.legs.values()), 4)
    run.counts = counts(await uat_run_repository.results_for_run(db, run.id))
    failed = [n for n, l in run.legs.items() if l.get("status") == "failed"]
    if run.status == "running":
        run.status = "awaiting_review"
        if failed:
            run.status_reason = "runner failed on " + ", ".join(
                f"{n} (exit {run.legs[n].get('exit_code')})" for n in failed)
    await uat_run_repository.save(db, run)
