"""MCP server a headless UAT runner uses to read its run and record results.

One server process serves one leg of one run: the run id, leg and evidence
directory come from its environment, set by ``testplan uat-run``, so a runner
cannot write to another run. It has no tracker tools at all.

Every result is appended to ``<evidence>/results.jsonl`` before it is sent.
When the backend cannot take it, the tool answers ``spooled`` and the runner
carries on; ``finish_run`` (and the CLI after the runner exits) replays every
unacknowledged line. The backend dedupes on ``client_id``, so a replay of a
result that did land writes nothing.

Environment:
    TESTPLAN_API_URL        backend base URL (default http://localhost:8000)
    UAT_RUN_ID, UAT_LEG     the run and leg this process serves
    UAT_EVIDENCE_DIR        absolute evidence directory for the run
    UAT_HTTP_ALLOWED_HOSTS  comma-separated hosts http_request may call
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("testplan-uat")

_SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
_BODY_LIMIT = 20_000


def _env() -> dict:
    return {
        "api": os.environ.get("TESTPLAN_API_URL", "http://localhost:8000").rstrip("/"),
        "run_id": os.environ.get("UAT_RUN_ID", ""),
        "leg": os.environ.get("UAT_LEG", ""),
        "evidence": os.environ.get("UAT_EVIDENCE_DIR", ""),
        "hosts": {h.strip().lower() for h in os.environ.get("UAT_HTTP_ALLOWED_HOSTS", "").split(",") if h.strip()},
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------


def spool_path(evidence_dir: str) -> Path:
    return Path(evidence_dir) / "results.jsonl"


def _append(evidence_dir: str, entry: dict) -> None:
    path = spool_path(evidence_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())


def pending(evidence_dir: str) -> list[dict]:
    """Spooled results the backend has neither acknowledged nor refused."""
    path = spool_path(evidence_dir)
    if not path.exists():
        return []
    results: dict[str, dict] = {}
    settled: set[str] = set()
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        cid = entry.get("client_id")
        if entry.get("type") == "result" and cid:
            results[cid] = entry
        elif entry.get("type") in ("ack", "refused") and cid:
            settled.add(cid)
    return [e for cid, e in results.items() if cid not in settled]


async def send(client: httpx.AsyncClient, env: dict, entry: dict) -> dict:
    """Send one spooled result. Returns {"state": ack|refused|spooled, ...}."""
    url = f"{env['api']}/uat-runs/{env['run_id']}/legs/{env['leg']}/results"
    try:
        r = await client.post(url, json=entry["payload"], timeout=20)
    except httpx.HTTPError as e:
        return {"state": "spooled", "why": f"backend unreachable ({type(e).__name__})"}
    if r.status_code >= 500:
        return {"state": "spooled", "why": f"backend answered {r.status_code}"}
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:500]
        _append(env["evidence"], {"type": "refused", "client_id": entry["client_id"],
                                  "status": r.status_code, "detail": detail, "at": _now()})
        return {"state": "refused", "status": r.status_code, "detail": detail}
    _append(env["evidence"], {"type": "ack", "client_id": entry["client_id"], "at": _now(),
                              "duplicate": bool(r.json().get("duplicate"))})
    return {"state": "ack", **r.json()}


async def replay(env: dict) -> dict:
    """Send every pending spooled result again. Safe to repeat."""
    out = {"sent": 0, "refused": [], "still_spooled": 0}
    async with httpx.AsyncClient() as client:
        for entry in pending(env["evidence"]):
            res = await send(client, env, entry)
            if res["state"] == "ack":
                out["sent"] += 1
            elif res["state"] == "refused":
                out["refused"].append({"case_id": entry["payload"].get("case_id"), "detail": res["detail"]})
            else:
                out["still_spooled"] += 1
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


_EVIDENCE_SCHEMA = {
    "type": "array",
    "description": "What shows the result. Required for passed and failed.",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["assertion", "screenshot", "video_ts", "unit_test", "http"]},
            "ref": {"type": "string", "description": "File path under the evidence dir, timestamp, or http evidence ref"},
            "at_s": {"type": "number", "description": "Seconds into the walkthrough video"},
            "text": {"type": "string", "description": "What was observed, in one sentence"},
        },
        "required": ["kind"],
    },
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_uat_run",
            description=(
                "Your run: the pinned plan version, an index of the cases this leg must run (id, title, "
                "surface, each repo's build status, any result already recorded — skip those), the "
                "video walkthrough chapters, and the evidence directory. Call this first, then "
                "get_case for each case's full content. Do not read the plan from the tracker."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_case",
            description=(
                "One case in full: steps, preconditions, test data, expected result, and the builds of "
                "the repos it exercises. Read each case with this before running it."
            ),
            inputSchema={
                "type": "object",
                "properties": {"case_id": {"type": "string", "description": "Canonical id from get_uat_run"}},
                "required": ["case_id"],
            },
        ),
        Tool(
            name="record_case_result",
            description=(
                "Record one case's result as soon as it is known. passed and failed need evidence; "
                "not_run needs not_run.code and not_run.detail. Answers 'recorded', 'spooled' (the "
                "backend is down; it is kept and replayed, carry on) or 'refused' with the reason "
                "(fix the call; nothing was recorded)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "Canonical id from get_uat_run, e.g. happy_path:0"},
                    "status": {"type": "string", "enum": ["passed", "failed", "not_run"]},
                    "evidence": _EVIDENCE_SCHEMA,
                    "not_run": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}, "detail": {"type": "string"}},
                    },
                    "flaky": {"type": "boolean", "description": "Passed only on a retry"},
                    "attempts": {"type": "integer"},
                    "started_at": {"type": "string"},
                    "finished_at": {"type": "string"},
                },
                "required": ["case_id", "status"],
            },
        ),
        Tool(
            name="should_continue",
            description=(
                "Call before each case. continue=false means stop now: the run was cancelled, the "
                "plan was replaced, or the ticket moved. Then call finish_run and end."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="finish_run",
            description=(
                "Call once at the end. Replays any spooled results and lists cases that still have "
                "no result. If any are listed, record each (a result or a not_run reason) and call "
                "finish_run again."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="http_request",
            description=(
                "Make one HTTP request for a request-level case, to an allowlisted host only. The "
                "request and response are saved under the evidence directory (secrets redacted); "
                "use the returned ref as http evidence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
                    "url": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "body": {"description": "JSON value or string"},
                    "label": {"type": "string", "description": "Short name for the evidence file, e.g. edge_cases-3-no-token"},
                },
                "required": ["method", "url"],
            },
        ),
    ]


def _text(obj: Any) -> list[TextContent]:
    return [TextContent(type="text", text=obj if isinstance(obj, str) else json.dumps(obj, indent=1))]


def _configured(env: dict) -> str | None:
    missing = [k for k, v in (("UAT_RUN_ID", env["run_id"]), ("UAT_LEG", env["leg"]),
                              ("UAT_EVIDENCE_DIR", env["evidence"])) if not v]
    if missing:
        return "This server is not bound to a run (unset: " + ", ".join(missing) + "). Stop and report it."
    return None


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    env = _env()
    problem = _configured(env)
    if problem:
        return _text({"error": "not_configured", "message": problem})
    args = arguments or {}
    if name == "get_uat_run":
        return _text(compact(await _get_run(env), hosts=env["hosts"]))
    if name == "get_case":
        return _text(await _get_case(env, args.get("case_id") or ""))
    if name == "record_case_result":
        return _text(await _record(env, args))
    if name == "should_continue":
        return _text(await _continue(env))
    if name == "finish_run":
        return _text(await _finish(env))
    if name == "http_request":
        return _text(await _http(env, args))
    raise ValueError(f"Unknown tool: {name}")


async def _get_run(env: dict) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{env['api']}/uat-runs/{env['run_id']}", params={"leg": env["leg"]}, timeout=30)
    except httpx.HTTPError as e:
        return {"error": "backend_unreachable",
                "message": f"The run could not be read ({type(e).__name__}). Stop: without it no case can run."}
    if r.status_code != 200:
        return {"error": "run_unreadable", "status": r.status_code, "message": r.text[:1000]}
    return r.json()


def compact(run: dict, hosts: set[str] | frozenset[str] = frozenset()) -> dict:
    """The run without each case's full content, which get_case returns.

    The full view of a 49-case plan is ~137k characters: past what one tool
    result may hold, so the runner had to page it back in from a file.
    """
    if run.get("error"):
        return run
    out = {k: v for k, v in run.items() if k not in ("cases", "legs", "builds")}
    leg = (run.get("legs") or {}).get(run.get("leg")) or {}
    out["leg_info"] = {k: leg.get(k) for k in ("repos", "surfaces", "agent", "status")}
    # The hosts http_request will call. On the first real run the runner did
    # not know one was allowed and wrote off every request-level case.
    out["http_allowed_hosts"] = sorted(hosts)
    out["builds"] = [{k: b.get(k) for k in ("repo", "status", "env", "detail")} for b in run.get("builds") or []]
    out["cases"] = [{
        "id": c["id"], "section": c.get("section"), "title": c.get("title"), "surface": c.get("surface"),
        "builds": {b["repo"]: b.get("status") for b in c.get("builds") or []},
        "result": (c.get("result") or {}).get("status"),
    } for c in run.get("cases") or []]
    return out


async def _get_case(env: dict, case_id: str) -> dict:
    run = await _get_run(env)
    if run.get("error"):
        return run
    for c in run.get("cases") or []:
        if c["id"] == case_id:
            return c
    return {"error": "unknown_case_id", "message": f"{case_id!r} is not one of this leg's cases",
            "known": [c["id"] for c in run.get("cases") or []]}


async def _record(env: dict, args: dict) -> dict:
    payload = {k: args.get(k) for k in ("case_id", "status", "not_run", "flaky", "attempts",
                                        "started_at", "finished_at") if args.get(k) is not None}
    payload["evidence"] = args.get("evidence") or []
    payload["client_id"] = str(uuid.uuid4())
    entry = {"type": "result", "client_id": payload["client_id"], "payload": payload, "at": _now()}
    _append(env["evidence"], entry)
    async with httpx.AsyncClient() as client:
        res = await send(client, env, entry)
    if res["state"] == "ack":
        return {"result": "recorded", "case_id": payload.get("case_id"), "duplicate": res.get("duplicate", False)}
    if res["state"] == "refused":
        return {"result": "refused", "case_id": payload.get("case_id"), "detail": res["detail"],
                "message": "Nothing was recorded. Fix the call and record this case again."}
    return {"result": "spooled", "case_id": payload.get("case_id"), "why": res["why"],
            "message": "Kept locally and replayed later. Carry on with the next case."}


async def _continue(env: dict) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{env['api']}/uat-runs/{env['run_id']}/continue", timeout=30)
        if r.status_code == 200:
            return r.json()
        why = f"backend answered {r.status_code}"
    except httpx.HTTPError as e:
        why = f"backend unreachable ({type(e).__name__})"
    # The backend being down does not stop a run: results spool. The answer
    # says the check was not made rather than that it passed.
    return {"continue": True, "reason": None, "checks": {"unchecked": why}}


async def _finish(env: dict) -> dict:
    replayed = await replay(env)
    run = await _get_run(env)
    if run.get("error"):
        return {"replayed": replayed, "missing": None,
                "message": "The run could not be read to check for missing results; the CLI will "
                           "replay the spool and close the leg after you exit."}
    missing = run.get("missing") or []
    return {
        "replayed": replayed,
        "missing": missing,
        "message": ("Every case has a result. End now." if not missing and not replayed["still_spooled"]
                    else "Record a result or a not_run reason for each missing case, then call finish_run again."
                    if missing else "Some results are still spooled; the CLI replays them after you exit. End now."),
    }


def _redact(headers: dict) -> dict:
    return {k: ("<redacted>" if k.lower() in _SECRET_HEADERS else v) for k, v in (headers or {}).items()}


async def _http(env: dict, args: dict) -> dict:
    url = args.get("url") or ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return {"error": "bad_url", "message": "url must be an absolute http(s) URL"}
    if host not in env["hosts"]:
        return {"error": "host_not_allowed",
                "message": f"{host} is not in this leg's http_allowed_hosts. Record the case as not_run "
                           f"permission_denied naming the host; do not try another route."}
    method = (args.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}
    body = args.get("body")
    kwargs: dict[str, Any] = {"headers": headers}
    if body is not None:
        if isinstance(body, (dict, list)):
            kwargs["json"] = body
        else:
            kwargs["content"] = str(body)
    started = _now()
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            r = await client.request(method, url, timeout=30, **kwargs)
        response = {"status": r.status_code, "headers": _redact(dict(r.headers)), "body": r.text[:_BODY_LIMIT],
                    "truncated": len(r.text) > _BODY_LIMIT}
    except httpx.HTTPError as e:
        response = {"error": f"{type(e).__name__}: {e}"}
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.get("label") or "request").strip("-")[:60] or "request"
    out_dir = Path(env["evidence"]) / "http"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(out_dir.glob("*.json"))) + 1
    ref = out_dir / f"{n:03d}-{label}.json"
    ref.write_text(json.dumps({"at": started, "request": {"method": method, "url": url,
                                                          "headers": _redact(headers), "body": body},
                               "response": response}, indent=1))
    return {"ref": str(ref), **response}


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
