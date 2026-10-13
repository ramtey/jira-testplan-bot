"""Look past the diff before a plan is written: sibling routes, callers, guards.

The generator only ever saw the files a PR changed, and the facts a tester
most needs often live elsewhere. On SK-2687 every bot plan missed that
`POST /api/signerToken` — a different repo, sharing no name with the diff —
still mints a signer token for any envelope, which undoes the ticket's
owner-only rule. A hand-run agent found it by asking "what else mints a signer
token?" and reading the answer. Symbol-following cannot ask that question, so
this is a small agent: Claude with read-only code search and file reads, a
hard budget, and one way to finish — `report_findings`.

Two rules keep its output honest:

* A finding may only cite a file the explorer actually read in this run.
  Anything else is dropped before it reaches the generator, so a guessed
  location never becomes a cited "fact".
* Files the ticket's PRs touched are read at the PR's own commit; other files
  at the repo's default branch. Reads are confined to the organizations the
  PRs belong to.

Failure is reported, never silent: `error` says why exploration did not run
or did not finish, and the plan records it as a context gap.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .diff_budget import allocate_patch_budget

logger = logging.getLogger(__name__)

MAX_TURNS = 10
MAX_SEARCHES = 8
MAX_READS = 14
READ_MAX_LINES = 300
MAX_FINDINGS = 12

FINDING_KINDS = ["sibling_entry_point", "caller", "guard", "config", "reachability", "other"]

TOOLS = [
    {
        "name": "search_code",
        "description": (
            "Search code across the organization (or one repo) with GitHub code search. "
            "Returns matching files with short snippets. Use specific identifiers, route "
            "fragments or phrases (e.g. `signerToken`, `SigningMethod InPerson`), not prose. "
            f"At most {MAX_SEARCHES} searches per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms, GitHub code search syntax."},
                "repo": {"type": "string", "description": "Optional owner/repo to search within."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a file, line-numbered. Files the ticket's PRs changed are read at the PR's "
            f"commit; others at the default branch. Up to {READ_MAX_LINES} lines per call; "
            f"at most {MAX_READS} reads per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/repo"},
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
            },
            "required": ["repo", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "report_findings",
        "description": (
            "Finish: report what a tester must know about code OUTSIDE the PR diffs. Only cite "
            "files you read with read_file in this run. Call this exactly once, last."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": FINDING_KINDS},
                            "repo": {"type": "string"},
                            "path": {"type": "string"},
                            "lines": {"type": "string", "description": "e.g. 57-94"},
                            "what_it_does": {"type": "string"},
                            "why_it_matters": {"type": "string"},
                            "suggested_check": {"type": "string"},
                        },
                        "required": ["kind", "repo", "path", "lines", "what_it_does",
                                     "why_it_matters", "suggested_check"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["findings"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = """You explore a codebase before a QA test plan is written, to find what the pull requests' diffs do not show.

Look for, in this order of value:
1. Sibling entry points: other routes, handlers, UI actions or services that perform the SAME capability the ticket changes (another endpoint that mints the same token, another screen that offers the same action). Check whether each enforces the ticket's new rule. A sibling that does not is the most important thing you can find.
2. Guards and preconditions on the changed path that live in unchanged code: status checks, permission checks, feature flags, ordering rules. Note their exact responses (status codes, messages).
3. Callers and consumers of what the PRs changed, where behaviour visible to a user depends on them.
4. Reachability: for each condition a changed branch handles (an error type or message, a regex alternative, a defensive guard, an input format), check whether anything in the repo, or the framework as this repo uses it, actually produces it. Search for the producer: the code that raises that error, sends that input, or loads things the way the branch assumes. Report a branch nothing reaches today as kind "reachability" with what you searched, so the plan covers it with a unit test instead of a UI case.

Work like an engineer: search for specific identifiers, read the files that matter, follow one hop further when it pays. Prefer reading over guessing. Skip anything the diffs already show.

Finish by calling report_findings once. Cite only files you read, with real line numbers. Report nothing rather than something you did not verify; an empty list is a valid answer. Every finding must say what a tester should check."""


def _orgs(pull_requests: list[dict]) -> list[str]:
    return sorted({(pr.get("repository") or "").split("/")[0]
                   for pr in pull_requests if pr.get("repository")} - {""})


def build_brief(summary: str, description: str | None, pull_requests: list[dict],
                diff_budget: int = 30_000) -> str:
    """What the explorer starts from: the ticket, its PRs, and their diffs."""
    lines = [f"Ticket: {summary}", "", (description or "")[:3000], "", "Pull requests:"]
    for pr in pull_requests:
        lines.append(f"- {pr.get('repository')} #{pr.get('number')} ({pr.get('status')}): "
                     f"{pr.get('title') or ''}")
        for f in (pr.get("files_changed") or [])[:40]:
            lines.append(f"    {f.get('status')} {f.get('filename')}")
    lines.append("")
    lines.append("Diffs (may be cut to fit):")
    per_pr = diff_budget // max(1, len(pull_requests))
    for pr in pull_requests:
        shown = allocate_patch_budget(pr.get("files_changed") or [], total_budget=per_pr,
                                      per_file_cap=6000)
        for name, patch, cut in shown:
            lines.append(f"--- {pr.get('repository')}:{name}{' (cut)' if cut else ''}\n{patch}")
    return "\n".join(lines)


class Workspace:
    """The explorer's tools, with their budgets and the record of what was read."""

    def __init__(self, github, pull_requests: list[dict], *,
                 max_searches: int = MAX_SEARCHES, max_reads: int = MAX_READS):
        self.github = github
        self.max_searches, self.max_reads = max_searches, max_reads
        self.orgs = _orgs(pull_requests)
        self.pr_refs: dict[tuple[str, str], str] = {}
        for pr in pull_requests:
            sha = pr.get("head_sha")
            for f in pr.get("files_changed") or []:
                if sha and pr.get("repository"):
                    self.pr_refs[(pr["repository"], f.get("filename"))] = sha
        self.searches = 0
        self.reads = 0
        self.read_files: set[tuple[str, str]] = set()

    def _in_scope(self, repo: str) -> bool:
        return bool(repo) and repo.split("/")[0] in self.orgs

    async def search_code(self, query: str, repo: str | None = None) -> str:
        if self.searches >= self.max_searches:
            return "Search budget spent. Read what you have found, then report."
        if repo and not self._in_scope(repo):
            return f"{repo} is outside the organizations this ticket belongs to."
        self.searches += 1
        scope = f"repo:{repo}" if repo else " ".join(f"org:{o}" for o in self.orgs)
        from .github_client import GitHubSearchThrottled
        try:
            hits = await self.github.search_code_hits(query, scope)
        except GitHubSearchThrottled as e:
            return f"Search unavailable right now ({e}). This is not 'no matches'."
        if not hits:
            return "No matches."
        return "\n".join(
            f"{h['repo']}:{h['path']}\n" + "\n".join(f"    | {frag}" for frag in h["fragments"])
            for h in hits
        )

    async def read_file(self, repo: str, path: str, start_line: int = 1) -> str:
        if self.reads >= self.max_reads:
            return "Read budget spent. Report what you have."
        if not self._in_scope(repo):
            return f"{repo} is outside the organizations this ticket belongs to."
        self.reads += 1
        ref = self.pr_refs.get((repo, path))
        files = await self.github.fetch_files_at_ref(repo, ref, [path])
        if path not in files:
            return f"Could not read {repo}:{path}" + (f" at {ref[:7]}" if ref else "") + "."
        self.read_files.add((repo, path))
        all_lines = files[path].splitlines()
        start = max(1, start_line)
        end = min(len(all_lines), start + READ_MAX_LINES - 1)
        body = "\n".join(f"{n:5d}| {all_lines[n - 1]}" for n in range(start, end + 1))
        tail = f"\n… lines {end + 1}-{len(all_lines)} not shown" if end < len(all_lines) else ""
        at = f"PR commit {ref[:7]}" if ref else "default branch"
        return f"{repo}:{path} ({at}, lines {start}-{end} of {len(all_lines)})\n{body}{tail}"

    def keep(self, findings: list) -> tuple[list[dict], int]:
        """Findings that cite a file read in this run; and how many were dropped."""
        kept = [f for f in findings if isinstance(f, dict)
                and (f.get("repo"), f.get("path")) in self.read_files]
        return kept[:MAX_FINDINGS], len(findings) - len(kept)


async def run_tool_loop(llm, ws: "Workspace", *, system: str, first_message: str,
                        report_tool: dict, max_turns: int, result: dict) -> dict | None:
    """Let the model search and read through ``ws`` until it calls ``report_tool``.

    Returns the report's input, or None (with ``result["error"]`` set) when it
    never reported. Fills ``turns`` and ``usage`` in ``result``; raises only
    what ``llm._post_messages`` raises.
    """
    tools = TOOLS[:2] + [report_tool]
    report_name = report_tool["name"]
    messages = [{"role": "user", "content": first_message}]
    for turn in range(max_turns + 1):
        result["turns"] = turn + 1
        last = turn == max_turns
        if last:
            messages.append({"role": "user", "content":
                             f"Budget reached. Call {report_name} now with what you verified."})
        data = await llm._post_messages(
            {
                "model": llm.model,
                "max_tokens": llm._budget(4096, thinking=8192),
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": messages,
                "tools": tools,
                "tool_choice": {"type": "auto"},
                # Each turn re-sends the whole history; caching its end
                # makes the next turn pay full price for the new part only.
                "cache_control": {"type": "ephemeral"},
                **llm._effort_kwargs("medium"),
            },
            timeout=180,
            retry_timeouts=True,
        )
        for k, v in (data.get("usage") or {}).items():
            if k in result["usage"] and isinstance(v, int):
                result["usage"][k] += v
        content = data.get("content") or []
        # Back unchanged, thinking blocks included: the next turn continues this one.
        messages.append({"role": "assistant", "content": content})
        calls = [b for b in content if b.get("type") == "tool_use"]
        report = next((b for b in calls if b.get("name") == report_name), None)
        if report:
            return report.get("input") or {}
        if not calls:
            if last:
                result["error"] = f"stopped without calling {report_name}"
                return None
            messages.append({"role": "user", "content": f"Call {report_name} to finish."})
            continue

        async def run(block):
            args = block.get("input") or {}
            try:
                if block.get("name") == "search_code":
                    out = await ws.search_code(args.get("query", ""), args.get("repo"))
                elif block.get("name") == "read_file":
                    out = await ws.read_file(args.get("repo", ""), args.get("path", ""),
                                             int(args.get("start_line") or 1))
                else:
                    out = f"Unknown tool {block.get('name')}."
                return {"type": "tool_result", "tool_use_id": block["id"], "content": out}
            except Exception as e:  # a failed tool is told to the model, not raised
                return {"type": "tool_result", "tool_use_id": block["id"],
                        "content": f"Tool failed: {type(e).__name__}: {e}", "is_error": True}

        # All results in one user message, so parallel calls stay parallel.
        messages.append({"role": "user", "content": list(await asyncio.gather(*(run(b) for b in calls)))})
    result["error"] = "ran out of turns"
    return None


def _new_result() -> dict:
    return {"findings": [], "dropped": 0, "searches": 0, "reads": 0, "turns": 0, "error": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}


async def explore(llm, github, *, summary: str, description: str | None,
                  pull_requests: list[dict]) -> dict:
    """Run the explorer. Never raises.

    Returns {"findings", "dropped", "searches", "reads", "turns", "error"}.
    """
    result = _new_result()
    prs = [pr for pr in pull_requests if pr.get("repository")]
    if not prs:
        result["error"] = "no pull requests with a repository to explore from"
        return result
    ws = Workspace(github, prs)
    try:
        report = await run_tool_loop(
            llm, ws, system=SYSTEM, first_message=build_brief(summary, description, prs),
            report_tool=TOOLS[2], max_turns=MAX_TURNS, result=result)
        if report is not None:
            kept, dropped = ws.keep(report.get("findings") or [])
            result.update(findings=kept, dropped=dropped)
        elif result["error"] == "ran out of turns":
            result["error"] = "explorer ran out of turns"
        else:
            result["error"] = "explorer stopped without reporting findings"
    except Exception as e:
        logger.warning("code exploration failed: %s", e)
        result["error"] = f"{type(e).__name__}: {e}"
    result["searches"], result["reads"] = ws.searches, ws.reads
    return result


def render_findings(exploration: dict | None) -> str:
    """The prompt section; empty when exploration was off or found nothing."""
    findings = (exploration or {}).get("findings") or []
    if not findings:
        return ""
    lines = []
    for f in findings:
        lines.append(f"- [{f['kind']}] {f['repo']}:{f['path']}:{f['lines']} — {f['what_it_does']}")
        lines.append(f"    Why it matters: {f['why_it_matters']}")
        lines.append(f"    Check: {f['suggested_check']}")
    return "\n".join(lines)


EXPLORATION_GUIDANCE = (
    "These findings come from reading code outside the PR diffs. Turn each into a case or a "
    "risk: a sibling entry point gets its own case expecting the ticket's rule (from the AC), "
    "never its current behaviour; one that does not enforce the rule is also a risks_and_gaps "
    "entry. Cite the file:line given here in expected_source. A \"reachability\" finding says "
    "nothing in the app produces a condition the diff handles: cover that branch as unit coverage "
    "(rule L), never as a UI case."
)


def summary_line(exploration: dict) -> str:
    """One line for logs and provenance."""
    return json.dumps({k: exploration.get(k) for k in ("searches", "reads", "turns", "dropped", "error", "usage")}
                      | {"findings": len(exploration.get("findings") or [])})
