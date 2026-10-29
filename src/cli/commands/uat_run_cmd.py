"""uat-run command — run a ticket's live plan with the headless UAT runner.

Starts a run through the backend (which pins the plan, checks each repo's
build and routes every case to a runner profile), then runs each leg's agent
headless with its allowlist and this repo's results server, and closes the
leg. Nothing is posted to the tracker: the run ends awaiting review.

The backend must be running: it is the one producer of runs and results.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx
import typer
from rich.console import Console
from typing_extensions import Annotated

from ...app.config import settings
from ...app.services.uat_runs import RunRefused, load_profiles
from ...uat_mcp import server as uat_mcp

console = Console()

MCP_NAME = "testplan-uat"


def _api() -> str:
    return os.environ.get("TESTPLAN_API_URL", "http://localhost:8000").rstrip("/")


def _fail(message: str) -> None:
    console.print(f"[bold red]✗[/bold red] {message}")
    raise typer.Exit(1)


def _post(path: str, payload: dict, timeout: float = 30) -> dict:
    try:
        r = httpx.post(f"{_api()}{path}", json=payload, timeout=timeout)
    except httpx.HTTPError as e:
        _fail(f"backend at {_api()} unreachable ({type(e).__name__}). Start it, then retry.")
    return _answer(path, r)


def _get(path: str, timeout: float = 30) -> dict:
    try:
        r = httpx.get(f"{_api()}{path}", timeout=timeout)
    except httpx.HTTPError as e:
        _fail(f"backend at {_api()} unreachable ({type(e).__name__}). Start it, then retry.")
    return _answer(path, r)


def _answer(path: str, r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text[:300]}
    if r.status_code == 404 and body.get("detail") == "Not Found":
        _fail("the backend predates the UAT routes; restart it, then retry.")
    if r.status_code >= 400:
        detail = body.get("detail")
        msg = detail.get("message") if isinstance(detail, dict) else detail
        _fail(f"{path}: {r.status_code} {msg}")
    return body


def _mcp_command() -> str:
    found = shutil.which("testplan-uat-mcp")
    if found:
        return found
    local = Path(__file__).resolve().parents[3] / ".venv" / "bin" / "testplan-uat-mcp"
    if local.exists():
        return str(local)
    _fail("testplan-uat-mcp is not installed (run `uv sync`).")


def build_prompt(run: dict, leg: str, profile: dict) -> str:
    """The run prompt. Short on purpose: the run itself comes from get_uat_run."""
    lines = [
        f"Run the UAT cases for {run['ticket_key']} (run {run['run_id']}, leg {leg}).",
        "",
        "mode: headless",
        f"dry_run: {str(run.get('dry_run', False)).lower()}",
        f"evidence_dir: {run['evidence_dir']}",
    ]
    for k, v in (profile.get("context") or {}).items():
        lines.append(f"{k}: {v}")
    lines += [
        "",
        f"Your run, cases and build answers come from the {MCP_NAME} tools, not from the tracker:",
        "1. get_uat_run first. Run only the cases it lists; skip any that already have a result.",
        "   Each case carries the builds of the repos it exercises; do not check the build yourself.",
        "2. should_continue before each case; stop when it says so.",
        "3. record_case_result as soon as each case is known (evidence for passed/failed,",
        "   a not_run code and detail otherwise). A refusal means nothing was recorded: fix and resend.",
        "4. finish_run at the end; record every case it lists as missing, then call it again.",
        "Do not mark progress any other way and do not write to the tracker.",
    ]
    return "\n".join(lines)


def _last_result(stream: Path) -> dict:
    """The final `result` event of a stream-json transcript, or {}."""
    if not stream.exists():
        return {}
    last = {}
    for line in stream.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            last = ev
    return last


def _check_profile(name: str, profile: dict) -> str | None:
    for key in ("agent", "cwd", "settings"):
        if not profile.get(key):
            return f"profile {name} has no {key}"
    if not Path(os.path.expanduser(profile["cwd"])).is_dir():
        return f"profile {name}: cwd {profile['cwd']} does not exist"
    if not Path(os.path.expanduser(profile["settings"])).is_file():
        return f"profile {name}: settings {profile['settings']} does not exist"
    return None


def run_leg(run: dict, leg: str, profile: dict, *, wall_clock_min: int, budget_usd: float | None) -> dict:
    """Run one leg's agent to completion (or the wall-clock cap) and return
    what finalize needs. Never raises for a runner failure: it is reported."""
    evidence = Path(run["evidence_dir"])
    evidence.mkdir(parents=True, exist_ok=True)
    env_vars = {"TESTPLAN_API_URL": _api(), "UAT_RUN_ID": str(run["run_id"]), "UAT_LEG": leg,
                "UAT_EVIDENCE_DIR": str(evidence),
                "UAT_HTTP_ALLOWED_HOSTS": ",".join(profile.get("http_allowed_hosts") or [])}
    mcp_config = evidence / f"mcp-{leg}.json"
    mcp_config.write_text(json.dumps({"mcpServers": {MCP_NAME: {
        "command": _mcp_command(), "args": [], "env": env_vars}}}, indent=1))
    stream = evidence / f"stream-{leg}.jsonl"
    cmd = ["claude", "-p", build_prompt(run, leg, profile),
           "--agent", profile["agent"],
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--settings", os.path.expanduser(profile["settings"]),
           "--mcp-config", str(mcp_config),
           "--allowedTools", f"mcp__{MCP_NAME}",
           "--output-format", "stream-json", "--verbose"]
    if profile.get("model"):
        cmd += ["--model", profile["model"]]
    started = time.monotonic()
    with stream.open("w") as out, open(os.devnull) as devnull:
        proc = subprocess.Popen(cmd, cwd=os.path.expanduser(profile["cwd"]), stdin=devnull,
                                stdout=out, stderr=subprocess.STDOUT)
        _post(f"/uat-runs/{run['run_id']}/legs/{leg}/start",
              {"pid": proc.pid, "host": socket.gethostname(), "model": profile.get("model")})
        console.print(f"  [dim]pid {proc.pid}, transcript {stream}[/dim]")
        capped = False
        try:
            proc.wait(timeout=wall_clock_min * 60)
        except subprocess.TimeoutExpired:
            capped = True
            console.print(f"  [yellow]wall-clock cap of {wall_clock_min} min reached: stopping[/yellow]")
            proc.send_signal(signal.SIGINT)  # first, so a recording can finalize
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=30)
    duration = round(time.monotonic() - started, 1)
    final = _last_result(stream)
    cost = float(final.get("total_cost_usd") or 0.0)
    info = {
        "exit_code": proc.returncode, "duration_s": duration, "cost_usd": cost,
        "session_id": final.get("session_id"),
        "permission_denials": final.get("permission_denials") or [],
    }
    if capped:
        info["missing_code"] = "dropped_time_cap"
        info["detail"] = f"wall-clock cap of {wall_clock_min} min reached"
    elif not final:
        info["detail"] = "the runner produced no result event; see the transcript"
    elif final.get("is_error"):
        info["detail"] = f"runner ended with {final.get('subtype')}: {str(final.get('result'))[:300]}"
    if budget_usd is not None and cost > budget_usd:
        info["budget_alert"] = f"leg cost ${cost:.2f}, over the ${budget_usd:.2f} run budget"
    # Results the runner spooled while the backend was away go in before the
    # leg closes, so finalize does not fill their cases as aborted.
    replay_env = {"api": _api(), "run_id": str(run["run_id"]), "leg": leg, "evidence": str(evidence),
                  "hosts": set()}
    info["replayed"] = asyncio.run(uat_mcp.replay(replay_env))
    return info


def _summary(view: dict) -> None:
    c = view.get("counts") or {}
    console.print(f"\n[bold]Run {view['run_id']}[/bold] {view['ticket_key']} plan {view['plan_id']} "
                  f"v{view['plan_version']}: [bold]{view['status']}[/bold]"
                  + (f" — {view['status_reason']}" if view.get("status_reason") else ""))
    console.print(f"  passed {c.get('passed', 0)}, failed {c.get('failed', 0)}, not run {c.get('not_run', 0)}")
    reasons = Counter((x["result"].get("not_run") or {}).get("code") for x in view["cases"]
                      if x.get("result") and x["result"]["status"] == "not_run")
    for code, n in reasons.most_common():
        console.print(f"    not run {code}: {n}")
    for x in view["cases"]:
        if x.get("result") and x["result"]["status"] == "failed":
            console.print(f"  [red]FAIL[/red] {x['id']} {x['title']}")
    for name, leg in (view.get("legs") or {}).items():
        denials = leg.get("permission_denials") or []
        console.print(f"  leg {name}: {leg.get('status')}, ${float(leg.get('cost_usd') or 0):.2f}, "
                      f"{leg.get('duration_s') or 0}s, {len(denials)} permission denial(s)"
                      + (f" — {leg['detail']}" if leg.get("detail") else ""))
        if leg.get("budget_alert"):
            console.print(f"  [bold yellow]budget alert:[/bold yellow] {leg['budget_alert']}")
    console.print(f"  evidence: {view.get('evidence_dir')}")


def uat_run(
    ticket_key: Annotated[str, typer.Argument(help="Ticket whose live plan to run")],
    leg: Annotated[str | None, typer.Option("--leg", help="Run only this runner profile's leg")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Tell the runner it is a dry run")] = False,
    route_only: Annotated[bool, typer.Option("--route-only", help="Start the run and show routing; launch no runner")] = False,
    resume: Annotated[int | None, typer.Option("--run", help="Resume this run: replay spooled results, close legs a runner already ran, launch pending ones")] = None,
):
    """
    Run a ticket's live plan with the headless UAT runner, ending awaiting review.

    Examples:
        testplan uat-run PROJ-123
        testplan uat-run PROJ-123 --leg mobile
        testplan uat-run PROJ-123 --route-only
        testplan uat-run PROJ-123 --run 42
    """
    try:
        profiles = load_profiles()
    except RunRefused as e:
        _fail(e.message)
    if resume is not None:
        run = _get(f"/uat-runs/{resume}")
        if run["ticket_key"] != ticket_key.upper():
            _fail(f"run {resume} is for {run['ticket_key']}, not {ticket_key.upper()}")
        if run["status"] != "running":
            _summary(run)
            return
    else:
        console.print(f"Starting a run for {ticket_key.upper()} (reading its PRs and builds)…")
        run = _post("/uat-runs", {"ticket_key": ticket_key, "dry_run": dry_run,
                                  "budget_usd": settings.uat_run_budget_usd}, timeout=180)
    for b in run["builds"]:
        console.print(f"  build {b['repo']}: {b['status']}" + (f" — {b['detail']}" if b.get("detail") else ""))
    for name, l in run["legs"].items():
        console.print(f"  leg {name}: {len(l['case_ids'])} case(s), agent {l.get('agent')}")
    routed_out = Counter((c.get("routed_not_run") or {}).get("code") for c in run["cases"] if c.get("routed_not_run"))
    for code, n in routed_out.most_common():
        console.print(f"  not runnable now — {code}: {n}")

    legs = [leg] if leg else list(run["legs"])
    if leg and leg not in run["legs"]:
        _fail(f"run {run['run_id']} has no leg {leg!r} (has: {', '.join(run['legs']) or 'none'})")
    if route_only or run["status"] != "running":
        if run["status"] == "running":
            console.print(f"\n[dim]--route-only: run {run['run_id']} left running; launch with "
                          f"`testplan uat-run {run['ticket_key']} --run {run['run_id']}`, or cancel with "
                          f"POST /uat-runs/{run['run_id']}/cancel[/dim]")
        _summary(run)
        return
    view = run
    for name in legs:
        state = run["legs"][name].get("status")
        if state in ("finished", "failed"):
            continue
        if state == "running":
            # A runner already ran (or died) on this leg. Rerunning it is P4's
            # job; here its spooled results go in and the leg closes.
            console.print(f"\n[bold]Leg {name}[/bold] (runner already started: replaying and closing)")
            rep = asyncio.run(uat_mcp.replay({"api": _api(), "run_id": str(run["run_id"]), "leg": name,
                                              "evidence": run["evidence_dir"], "hosts": set()}))
            if rep["still_spooled"]:
                _fail(f"{rep['still_spooled']} result(s) still spooled; the backend is not taking them.")
            view = _post(f"/uat-runs/{run['run_id']}/legs/{name}/finalize",
                         {"detail": "closed on resume after the runner exited"})
            continue
        profile = profiles.get(name) or {}
        problem = _check_profile(name, profile)
        console.print(f"\n[bold]Leg {name}[/bold]")
        if problem:
            console.print(f"  [red]{problem}[/red]")
            view = _post(f"/uat-runs/{run['run_id']}/legs/{name}/finalize",
                         {"exit_code": -1, "detail": problem})
            continue
        info = run_leg(run, name, profile, wall_clock_min=settings.uat_leg_wall_clock_min,
                       budget_usd=run.get("budget_usd") or settings.uat_run_budget_usd)
        rep = info.pop("replayed")
        if rep["sent"] or rep["refused"] or rep["still_spooled"]:
            console.print(f"  spool replay: {rep['sent']} sent, {len(rep['refused'])} refused, "
                          f"{rep['still_spooled']} still spooled")
        if rep["still_spooled"]:
            _fail(f"{rep['still_spooled']} result(s) are still spooled in {run['evidence_dir']}/results.jsonl "
                  f"and the backend is unreachable. The leg was not closed; once the backend is back run "
                  f"`testplan uat-run {run['ticket_key']} --run {run['run_id']}` (spooled results are "
                  "replayed, nothing reruns).")
        view = _post(f"/uat-runs/{run['run_id']}/legs/{name}/finalize", info)
    _summary(view)
