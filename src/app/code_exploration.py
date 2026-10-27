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
import re

from .diff_budget import allocate_patch_budget

logger = logging.getLogger(__name__)

MAX_TURNS = 14
MAX_SEARCHES = 12
MAX_READS = 18
READ_MAX_LINES = 300
MAX_FINDINGS = 14

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

_JOBS = {
    "siblings": """1. Sibling entry points: other routes, handlers, UI actions or services that perform the SAME capability the ticket changes (another endpoint that mints the same token, another screen that offers the same action). Check whether each enforces the ticket's new rule. A sibling that does not is the most important thing you can find.
2. Guards and preconditions on the changed path that live in unchanged code: status checks, permission checks, feature flags, ordering rules. Note their exact responses (status codes, messages).
3. Callers and consumers of what the PRs changed, where behaviour visible to a user depends on them.
4. Reachability: for each condition a changed branch handles (an error type or message, a regex alternative, a defensive guard, an input format), check whether anything in the repo, or the framework as this repo uses it, actually produces it. Search for the producer: the code that raises that error, sends that input, or loads things the way the branch assumes. Report a branch nothing reaches today as kind "reachability" with what you searched, so the plan covers it with a unit test instead of a UI case.""",
    "downstream": """1. Downstream flows in the packages the PRs did NOT change: what happens after the changed action hands off (for example the signer app after a hand-off, a worker after an enqueue). Visit every package listed in the brief at least once: the unchanged ones, and the changed ones (marked *) for their files the PRs did NOT touch, since a flow usually continues through untouched files of a package the PR edited elsewhere (signer-sdk's signature dialog after a change to its redirect helper). Search each for the feature's key terms and read what you find. Report steps a user meets there (consent or identity checks, confirmations, redirects).
2. The authentication setup the changed routes rely on (auth middleware, token validation, startup or policy config): what a caller with no token, a malformed token or a token for another account actually gets back, with the status code and the line that decides it.
3. Preconditions in UNCHANGED code that decide whether the changed control appears or works at all: ordering or grouping rules, states in which the control is hidden on purpose, and pre-existing behaviour a tester could misread as a regression.""",
}

_SYSTEM_HEAD = """You explore a codebase before a QA test plan is written, to find what the pull requests' diffs do not show. Another explorer covers other ground in parallel; stay on yours.

Look for, in this order of value:
"""
_SYSTEM_TAIL = """

Work like an engineer: search for specific identifiers, read the files that matter, follow one hop further when it pays. Prefer reading over guessing. Skip anything the diffs already show.

Finish by calling report_findings once. Cite only files you read, with real line numbers. Report nothing rather than something you did not verify; an empty list is a valid answer. Every finding must say what a tester should check."""

SYSTEMS = {name: _SYSTEM_HEAD + jobs + _SYSTEM_TAIL for name, jobs in _JOBS.items()}
SYSTEM = SYSTEMS["siblings"]  # the single-explorer brief, kept for callers that use it

#: Per explorer; two run in parallel.
PER_EXPLORER = {"max_turns": 10, "max_searches": 7, "max_reads": 10}


def _orgs(pull_requests: list[dict]) -> list[str]:
    return sorted({(pr.get("repository") or "").split("/")[0]
                   for pr in pull_requests if pr.get("repository")} - {""})


_MONOREPO_DIRS = ("packages", "apps", "libs", "services", "modules")
_NOT_PACKAGES = frozenset({"docs", "doc", "test", "tests", "scripts", "tools", "tooling", "build",
                           "dist", "node_modules", "infra", "deploy", "deployments", "ci", "assets",
                           "public", "fixtures", "e2e", "examples"})


def package_of(path: str) -> str | None:
    """The package or project a repo path belongs to: `packages/signer-sdk`
    in a monorepo, the top-level project folder otherwise (`DigiSign.Api`),
    None for root files."""
    segs = [s for s in path.split("/") if s]
    if len(segs) >= 3 and segs[0] in _MONOREPO_DIRS:
        return f"{segs[0]}/{segs[1]}"
    if len(segs) >= 2 and not segs[0].startswith("."):
        return segs[0]
    return None


def package_map(pull_requests: list[dict], trees: dict[str, list[str] | None]) -> dict[str, dict]:
    """repo → {"changed": [...], "others": [...]} packages, test projects left out."""
    out: dict[str, dict] = {}
    for pr in pull_requests:
        repo = pr.get("repository")
        if not repo:
            continue
        entry = out.setdefault(repo, {"changed": set(), "others": set()})
        for f in pr.get("files_changed") or []:
            pkg = package_of(f.get("filename") or "")
            if pkg:
                entry["changed"].add(pkg)
    for repo, entry in out.items():
        for path in trees.get(repo) or []:
            pkg = package_of(path)
            if pkg and pkg not in entry["changed"]:
                leaf = pkg.split("/")[-1].lower()
                if leaf in _NOT_PACKAGES or leaf.endswith((".tests", ".test", "tests", "-e2e")):
                    continue
                entry["others"].add(pkg)
    return {r: {"changed": sorted(e["changed"]), "others": sorted(e["others"])[:15]}
            for r, e in out.items()}


def render_packages(packages: dict[str, dict]) -> str:
    if not packages:
        return ""
    lines = ["Packages in each repo (the PRs changed the ones marked *):"]
    for repo, e in packages.items():
        names = [f"*{p}" for p in e["changed"]] + e["others"]
        lines.append(f"- {repo}: " + ", ".join(names))
    return "\n".join(lines)


def build_brief(summary: str, description: str | None, pull_requests: list[dict],
                diff_budget: int = 30_000, packages: dict[str, dict] | None = None) -> str:
    """What the explorer starts from: the ticket, its PRs, their diffs, and the
    packages around them."""
    lines = [f"Ticket: {summary}", "", (description or "")[:3000], "", "Pull requests:"]
    for pr in pull_requests:
        lines.append(f"- {pr.get('repository')} #{pr.get('number')} ({pr.get('status')}): "
                     f"{pr.get('title') or ''}")
        for f in (pr.get("files_changed") or [])[:40]:
            lines.append(f"    {f.get('status')} {f.get('filename')}")
    if packages:
        lines += ["", render_packages(packages)]
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


async def _explore_one(llm, github, prs: list[dict], *, name: str, brief: str) -> tuple[dict, list[dict]]:
    """One focused explorer. Returns its result record and the findings it kept."""
    result = _new_result()
    ws = Workspace(github, prs, max_searches=PER_EXPLORER["max_searches"],
                   max_reads=PER_EXPLORER["max_reads"])
    kept: list[dict] = []
    try:
        report = await run_tool_loop(
            llm, ws, system=SYSTEMS[name], first_message=brief,
            report_tool=TOOLS[2], max_turns=PER_EXPLORER["max_turns"], result=result)
        if report is not None:
            kept, result["dropped"] = ws.keep(report.get("findings") or [])
        elif result["error"] == "ran out of turns":
            result["error"] = f"{name} explorer ran out of turns"
        else:
            result["error"] = f"{name} explorer stopped without reporting findings"
    except Exception as e:
        logger.warning("code exploration (%s) failed: %s", name, e)
        result["error"] = f"{name} explorer: {type(e).__name__}: {e}"
    result["searches"], result["reads"] = ws.searches, ws.reads
    return result, kept


async def explore(llm, github, *, summary: str, description: str | None,
                  pull_requests: list[dict], explorers: tuple[str, ...] = tuple(SYSTEMS)) -> dict:
    """Run two focused explorers in parallel and merge what they found. Never raises.

    One run used to cover every job on one budget and spent it on the changed
    package, so code downstream of it (the signer app after a hand-off) was
    found in some runs and missed in others. One explorer now takes siblings,
    guards, callers and reachability; the other takes downstream packages,
    auth setup and hidden preconditions, and must visit every unchanged
    package. Findings are merged, one per file.

    Returns {"findings", "dropped", "searches", "reads", "turns", "error", "usage"}.
    """
    result = _new_result()
    prs = [pr for pr in pull_requests if pr.get("repository")]
    if not prs:
        result["error"] = "no pull requests with a repository to explore from"
        return result
    packages: dict[str, dict] = {}
    try:
        refs = {pr["repository"]: pr.get("head_sha") for pr in prs if pr.get("head_sha")}
        list_tree = getattr(github, "list_tree", None)
        trees = dict(zip(refs, await asyncio.gather(
            *(list_tree(repo, ref) for repo, ref in refs.items())))) if list_tree else {}
        packages = package_map(prs, trees)
    except Exception as e:  # without the map both explorers still run
        logger.warning("package map for exploration failed: %s", e)
    brief = build_brief(summary, description, prs, packages=packages)
    runs = await asyncio.gather(*(_explore_one(llm, github, prs, name=n, brief=brief) for n in explorers))
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    errors = []
    for (sub, kept) in runs:
        for k in ("searches", "reads", "turns", "dropped"):
            result[k] += sub.get(k) or 0
        for k, v in (sub.get("usage") or {}).items():
            result["usage"][k] = result["usage"].get(k, 0) + v
        if sub.get("error"):
            errors.append(sub["error"])
        for f in kept:
            key = (f.get("repo"), f.get("path"))
            if key not in seen:
                seen.add(key)
                merged.append(f)
    result["findings"] = merged[:MAX_FINDINGS]
    result["packages"] = packages
    if errors:
        result["error"] = "; ".join(errors)
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


# --- findings the plan dropped ------------------------------------------------
#
# On SK-2687 (2026-10-27) the explorer found the signer's identity checkbox,
# the active-group preconditions and the UI's 403/409 messages, and the plans
# still lacked them. Asking the main plan call to account for every finding
# (f02bd19) made that call fragile — empty plans in 3 of 8 attempts — so it was
# reverted. This leaves the main call untouched: a text match finds the
# findings no part of the plan mentions, and one separate call writes cases
# for those alone, before the critics, so they are checked like any other.

MAX_ADDED_CASES = 4
_ADD_SECTIONS = ("happy_path", "edge_cases", "integration_tests")
_GENERIC_STEMS = frozenset({"index", "utils", "util", "helpers", "helper", "types", "constants",
                            "config", "api", "service", "services", "hooks", "styles", "main"})
_BACKTICK_RE = re.compile(r"`([^`\s]{6,})`")


def _plan_text(test_plan) -> str:
    """Everything a tester or reviewer reads in the plan, as one string."""
    parts = []
    for key in ("happy_path", "edge_cases", "integration_tests", "security_negative_tests",
                "risks_and_gaps", "code_review_checks", "regression_checklist"):
        parts.append(json.dumps(getattr(test_plan, key, None) or [], default=str))
    parts.append(json.dumps(getattr(test_plan, "evidence_claims", None) or {}, default=str))
    return "\n".join(parts)


def finding_tokens(f: dict) -> list[str]:
    """Strings whose presence in the plan shows the finding was used: its file
    (with its folder when the name alone is generic) and the identifiers it
    quotes in backticks."""
    path = f.get("path") or ""
    segs = [s for s in path.split("/") if s]
    tokens = []
    if segs:
        base = segs[-1]
        stem = base.rsplit(".", 1)[0]
        if stem.lower() in _GENERIC_STEMS and len(segs) > 1:
            tokens.append(f"{segs[-2]}/{base}")
            if stem.lower() == "index":
                # SignatureSettingsModal/index.tsx: the folder is the component.
                # A utils.ts folder name (inPersonSigning) is everywhere, so not that.
                tokens.append(segs[-2])
        else:
            tokens.append(base)
            if len(stem) >= 8:
                tokens.append(stem)
    for field in ("what_it_does", "suggested_check"):
        tokens += _BACKTICK_RE.findall(f.get(field) or "")
    return [t for t in dict.fromkeys(tokens) if t and t.lower() not in _GENERIC_STEMS]


def dropped_findings(test_plan, exploration: dict | None) -> list[dict]:
    """Findings no part of the plan mentions. Reachability findings are left
    out: rule L routes those to unit coverage, not to a case."""
    text = _plan_text(test_plan)
    out = []
    for f in (exploration or {}).get("findings") or []:
        if f.get("kind") == "reachability":
            continue
        if not any(t in text for t in finding_tokens(f)):
            out.append(f)
    return out


def _repair_tool() -> dict:
    from .llm_client import TEST_CASE_SCHEMA  # llm_client imports this module
    case = {**TEST_CASE_SCHEMA, "properties": {
        **TEST_CASE_SCHEMA["properties"],
        "section": {"type": "string", "enum": list(_ADD_SECTIONS)},
        "covers_findings": {"type": "array", "items": {"type": "string"}},
    }}
    case["required"] = list(TEST_CASE_SCHEMA.get("required", [])) + ["section", "covers_findings"]
    return {
        "name": "add_cases",
        "description": "Cases for findings the plan does not cover yet.",
        "input_schema": {"type": "object", "properties": {"cases": {"type": "array", "items": case}},
                         "required": ["cases"]},
    }


REPAIR_SYSTEM = f"""You add test cases to an existing QA test plan. Each finding below was verified by reading the code it cites, and the plan does not mention it yet.

Write at most {MAX_ADDED_CASES} cases, the fewest that cover the findings worth testing for this ticket (one case may cover several when they are the same behaviour; skip a finding that does not belong to this ticket). Use "section": happy_path or edge_cases for something a tester does in the app, integration_tests for an API-level check, and list the finding ids each case covers in covers_findings. Take expected results from the cited file:line: set expected_verified true and expected_source to that file:line only when the cited code states the outcome; otherwise false. Never expect a behaviour the ticket is removing, and never repeat a case the plan already has (its titles are listed)."""


async def write_cases_for(llm, test_plan, findings: list[dict], *, summary: str) -> list[dict]:
    """One model call: cases for `findings`, each tagged with its section."""
    titles = [c.get("title", "") for s in _ADD_SECTIONS + ("security_negative_tests",)
              for c in (getattr(test_plan, s, None) or []) if isinstance(c, dict)]
    listed = []
    for f in findings:
        listed.append(f"- {f['id']} [{f['kind']}] {f['repo']}:{f['path']}:{f['lines']} — {f['what_it_does']}")
        listed.append(f"    Why it matters: {f['why_it_matters']}")
        listed.append(f"    Check: {f['suggested_check']}")
    brief = (f"Ticket: {summary}\n\nThe plan already has these cases:\n"
             + "\n".join(f"- {t}" for t in titles)
             + "\n\nFindings to cover:\n" + "\n".join(listed))
    data = await llm._post_messages(
        {
            "model": llm.model,
            "max_tokens": llm._budget(8192, thinking=8192),
            "system": REPAIR_SYSTEM,
            "messages": [{"role": "user", "content": brief}],
            "tools": [_repair_tool()],
            "tool_choice": {"type": "tool", "name": "add_cases"},
        },
        timeout=300,
        retry_timeouts=True,
    )
    block = next((b for b in data.get("content") or [] if b.get("type") == "tool_use"), None)
    cases = ((block or {}).get("input") or {}).get("cases") or []
    return [c for c in cases if isinstance(c, dict) and c.get("section") in _ADD_SECTIONS][:MAX_ADDED_CASES]


async def repair_dropped_findings(llm, test_plan, exploration: dict | None, *, summary: str) -> dict:
    """Write cases for the findings the plan dropped; return the provenance record.

    Never raises: a failed call keeps the plan as generated and says so.
    """
    findings = (exploration or {}).get("findings") or []
    if not findings:
        return {}
    for i, f in enumerate(findings, 1):
        f.setdefault("id", f"X{i}")
    dropped = dropped_findings(test_plan, exploration)
    dropped_ids = {f["id"] for f in dropped}
    outcome = {f["id"]: ("skipped_reachability" if f.get("kind") == "reachability"
                         else "dropped" if f["id"] in dropped_ids else "used") for f in findings}
    record: dict = {}
    if dropped:
        try:
            added = await write_cases_for(llm, test_plan, dropped, summary=summary)
        except Exception as e:
            logger.warning("could not write cases for dropped exploration findings: %s", e)
            added, record["repair_error"] = [], f"{type(e).__name__}: {e}"
        for case in added:
            section = case.pop("section")
            case["added_for_findings"] = True
            rows = getattr(test_plan, section, None)
            if rows is None:
                rows = []
                setattr(test_plan, section, rows)
            rows.append(case)
            for fid in case.get("covers_findings") or []:
                fid = str(fid).strip().upper()
                if outcome.get(fid) == "dropped":
                    outcome[fid] = f"added:{section}:{len(rows) - 1}"
        record["cases_added"] = len(added)
    record["finding_outcomes"] = [
        {"id": f["id"], "kind": f.get("kind"), "where": f"{f['repo']}:{f['path']}:{f['lines']}",
         "outcome": outcome[f["id"]]}
        for f in findings
    ]
    return record
