"""The explorer that looks past the diff before a plan is written.

Why this file exists
--------------------
Every SK-2687 bot plan missed that `POST /api/signerToken` — another repo,
sharing no name with the diff — still mints signer tokens for any envelope,
which undoes the ticket's owner-only rule. Code outside the changed files is
only reachable by searching and reading, so a small agent does that.

What can go quietly wrong, and is pinned here: a finding citing a file the
explorer never read reaching the generator as a "fact"; a PR's file being read
at the default branch instead of the PR's commit; reads wandering outside the
PRs' organizations; a throttled search reading as "no matches"; and a failure
raising instead of being reported.
"""

from __future__ import annotations

import pytest

from src.app.code_exploration import (
    MAX_TURNS,
    explore,
    render_findings,
)
from src.app.github_client import GitHubSearchThrottled

PRS = [{
    "repository": "acme/envelope-api", "number": 92, "status": "MERGED", "title": "Owner only",
    "head_sha": "6d9a355abc",
    "files_changed": [{"filename": "Api/StartInPersonSessionHandler.cs", "status": "modified",
                       "patch": "@@ -70 +70 @@\n+owner check"}],
}]

TOKEN_CONTROLLER = "\n".join(["using X;"] * 56 + ["// mints a signer token for any envelope"])


class FakeGitHub:
    def __init__(self, files=None, hits=None, throttle=False):
        self.files = files or {}
        self.hits = hits or []
        self.throttle = throttle
        self.reads: list[tuple[str, str | None, str]] = []

    async def search_code_hits(self, query, scope, max_hits=8):
        if self.throttle:
            raise GitHubSearchThrottled("rate limited", retry_after=60)
        return self.hits

    async def fetch_files_at_ref(self, repo, ref, paths):
        self.reads.append((repo, ref, paths[0]))
        key = f"{repo}:{paths[0]}"
        return {paths[0]: self.files[key]} if key in self.files else {}


class ScriptedLLM:
    """Plays back one assistant turn per call; records what it was sent."""

    model = "claude-test"

    def __init__(self, turns, raises=None):
        self.turns = list(turns)
        self.raises = raises
        self.sent: list[list] = []

    def _budget(self, visible, *, thinking):
        return visible + thinking

    def _effort_kwargs(self, level):
        return {}

    async def _post_messages(self, payload, **kw):
        if self.raises:
            raise self.raises
        self.sent.append(list(payload["messages"]))
        return {"content": self.turns.pop(0) if self.turns else [{"type": "text", "text": "done"}]}


def call(name, id_, **inp):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


def finding(repo, path, **over):
    return {"kind": "sibling_entry_point", "repo": repo, "path": path, "lines": "57-94",
            "what_it_does": "mints a signer token for any envelope",
            "why_it_matters": "bypasses the owner-only rule",
            "suggested_check": "As a TC, POST /api/signerToken for the owner's envelope", **over}


@pytest.mark.asyncio
async def test_finds_a_sibling_and_keeps_only_what_it_read():
    gh = FakeGitHub(files={"acme/user-service:Controllers/TokenController.cs": TOKEN_CONTROLLER},
                    hits=[{"repo": "acme/user-service", "path": "Controllers/TokenController.cs",
                           "fragments": ["signerToken"]}])
    llm = ScriptedLLM([
        [call("search_code", "t1", query="signerToken")],
        [call("read_file", "t2", repo="acme/user-service", path="Controllers/TokenController.cs")],
        [call("report_findings", "t3", findings=[
            finding("acme/user-service", "Controllers/TokenController.cs"),
            finding("acme/user-service", "Never/Read.cs"),  # cited without reading
        ])],
    ])
    result = await explore(llm, gh, summary="Owner only", description="", pull_requests=PRS)
    assert [f["path"] for f in result["findings"]] == ["Controllers/TokenController.cs"]
    assert result["dropped"] == 1
    assert result["searches"] == 1 and result["reads"] == 1 and result["error"] is None


@pytest.mark.asyncio
async def test_pr_files_are_read_at_the_pr_commit_others_at_default():
    gh = FakeGitHub(files={"acme/envelope-api:Api/StartInPersonSessionHandler.cs": "x",
                           "acme/envelope-api:Api/Other.cs": "y"})
    llm = ScriptedLLM([
        [call("read_file", "a", repo="acme/envelope-api", path="Api/StartInPersonSessionHandler.cs"),
         call("read_file", "b", repo="acme/envelope-api", path="Api/Other.cs")],
        [call("report_findings", "c", findings=[])],
    ])
    await explore(llm, gh, summary="s", description="", pull_requests=PRS)
    refs = {path: ref for _, ref, path in gh.reads}
    assert refs == {"Api/StartInPersonSessionHandler.cs": "6d9a355abc", "Api/Other.cs": None}
    # Both results went back in ONE user message, so parallel calls stay parallel.
    last_user = llm.sent[-1][-1]
    assert [b["tool_use_id"] for b in last_user["content"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_reads_outside_the_prs_organizations_are_refused():
    gh = FakeGitHub(files={"evil/repo:secrets.txt": "x"})
    llm = ScriptedLLM([
        [call("read_file", "a", repo="evil/repo", path="secrets.txt")],
        [call("report_findings", "b", findings=[finding("evil/repo", "secrets.txt")])],
    ])
    result = await explore(llm, gh, summary="s", description="", pull_requests=PRS)
    assert gh.reads == [] and result["findings"] == []
    assert "outside the organizations" in llm.sent[-1][-1]["content"][0]["content"]


@pytest.mark.asyncio
async def test_a_throttled_search_is_not_no_matches():
    llm = ScriptedLLM([[call("search_code", "a", query="signerToken")],
                       [call("report_findings", "b", findings=[])]])
    await explore(llm, FakeGitHub(throttle=True), summary="s", description="", pull_requests=PRS)
    told = llm.sent[-1][-1]["content"][0]["content"]
    assert "not 'no matches'" in told


@pytest.mark.asyncio
async def test_running_out_of_turns_is_reported():
    llm = ScriptedLLM([[call("search_code", f"s{i}", query="x")] for i in range(MAX_TURNS + 1)])
    result = await explore(llm, FakeGitHub(), summary="s", description="", pull_requests=PRS)
    assert result["error"] == "explorer ran out of turns"
    # The last turn told it to stop and report.
    assert "Call report_findings now" in str(llm.sent[-1])


@pytest.mark.asyncio
async def test_a_model_failure_is_reported_not_raised():
    result = await explore(ScriptedLLM([], raises=RuntimeError("overloaded")), FakeGitHub(),
                           summary="s", description="", pull_requests=PRS)
    assert result["error"] == "RuntimeError: overloaded" and result["findings"] == []


@pytest.mark.asyncio
async def test_no_prs_means_nothing_to_explore():
    result = await explore(ScriptedLLM([]), FakeGitHub(), summary="s", description="", pull_requests=[])
    assert result["error"] and result["turns"] == 0


def test_findings_reach_the_prompt_with_the_rule():
    from tests.test_bounce_in_prompts import _PromptOnlyClient
    exploration = {"findings": [finding("acme/user-service", "Controllers/TokenController.cs")]}
    assert "acme/user-service:Controllers/TokenController.cs:57-94" in render_findings(exploration)
    dev = {"pull_requests": [], "commits": [], "branches": [], "code_exploration": exploration}
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, dev)
    assert "Related code outside the PR diffs" in prompt
    assert "never its current behaviour" in prompt


def test_nothing_found_adds_nothing():
    assert render_findings({"findings": []}) == "" and render_findings(None) == ""


@pytest.mark.asyncio
async def test_history_is_cached_and_usage_is_summed():
    class UsageLLM(ScriptedLLM):
        async def _post_messages(self, payload, **kw):
            self.payloads = getattr(self, "payloads", []) + [payload]
            out = await super()._post_messages(payload, **kw)
            return {**out, "usage": {"input_tokens": 100, "output_tokens": 10,
                                     "cache_read_input_tokens": 50}}
    llm = UsageLLM([[call("search_code", "a", query="x")], [call("report_findings", "b", findings=[])]])
    result = await explore(llm, FakeGitHub(), summary="s", description="", pull_requests=PRS)
    assert all(p["cache_control"] == {"type": "ephemeral"} for p in llm.payloads)
    assert result["usage"]["input_tokens"] == 200 and result["usage"]["cache_read_input_tokens"] == 100


# ── every finding reaches the plan ─────────────────────────────────────────────

from src.app.code_exploration import account_for_findings, ensure_findings_covered  # noqa: E402
from src.app.models import TestPlan  # noqa: E402


def _plan(claims=None, **sections):
    return TestPlan(happy_path=sections.get("happy_path", [{"title": "owner hands off"}]),
                    edge_cases=sections.get("edge_cases", []), regression_checklist=[],
                    integration_tests=[], security_negative_tests=[], risks_and_gaps=[],
                    evidence_claims={"exploration_coverage": claims} if claims else None)


FOUND = {"findings": [
    {**finding("acme/ui", "signer/SignatureSettingsModal.tsx", kind="guard"), "id": "X1"},
    {**finding("acme/ui", "inPersonSigning/utils.ts", kind="guard"), "id": "X2"},
    {**finding("acme/api", "Auth/Startup.cs", kind="config"), "id": "X3"},
]}


def test_accounting_trusts_only_cases_that_exist():
    plan = _plan([{"finding": "X1", "covered_by": "happy_path:0"},
                  {"finding": "x2", "covered_by": "edge_cases:7"},          # past the end
                  {"finding": "X3", "out_of_scope_reason": "auth is SK-2910's"}])
    status = account_for_findings(plan, FOUND)
    assert status["X1"] == {"status": "covered", "by": "happy_path:0"}
    assert status["X2"]["status"] == "unaccounted"
    assert status["X3"] == {"status": "out_of_scope", "reason": "auth is SK-2910's"}


class RepairLLM(ScriptedLLM):
    def __init__(self, cases=None, raises=None):
        super().__init__([], raises=raises)
        self.cases = cases or []

    async def _post_messages(self, payload, **kw):
        if self.raises:
            raise self.raises
        self.payload = payload
        return {"content": [{"type": "tool_use", "id": "r", "name": "add_cases",
                             "input": {"cases": self.cases}}]}


@pytest.mark.asyncio
async def test_a_dropped_finding_becomes_a_case_before_the_critics():
    plan = _plan([{"finding": "X1", "covered_by": "happy_path:0"},
                  {"finding": "X3", "out_of_scope_reason": "not this ticket"}])
    llm = RepairLLM(cases=[{"section": "edge_cases", "covers_findings": ["X2"],
                            "title": "Hand-off link only for the active signing group",
                            "steps": ["Open history on a sequential envelope"],
                            "expected": "No link for signers in a later group", "priority": "medium",
                            "surface": "web_ui", "credentials": "owner", "expected_verified": False}])
    record = await ensure_findings_covered(llm, plan, FOUND, summary="Owner only")
    assert plan.edge_cases[-1]["title"].startswith("Hand-off link")
    assert plan.edge_cases[-1]["added_for_findings"] is True and "section" not in plan.edge_cases[-1]
    by_id = {f["id"]: f for f in record["finding_outcomes"]}
    assert by_id["X2"] == {**by_id["X2"], "status": "added", "by": "edge_cases:0"}
    assert by_id["X1"]["status"] == "covered" and by_id["X3"]["status"] == "out_of_scope"
    # Only the dropped finding went to the repair call, with the plan's titles.
    sent = llm.payload["messages"][0]["content"]
    assert "X2" in sent and "SignatureSettingsModal" not in sent and "owner hands off" in sent


@pytest.mark.asyncio
async def test_nothing_dropped_means_no_extra_call():
    plan = _plan([{"finding": "X1", "covered_by": "happy_path:0"},
                  {"finding": "X2", "covered_by": "happy_path:0"},
                  {"finding": "X3", "covered_by": "happy_path:0"}])
    llm = RepairLLM(raises=AssertionError("should not be called"))
    record = await ensure_findings_covered(llm, plan, FOUND, summary="s")
    assert all(f["status"] == "covered" for f in record["finding_outcomes"])


@pytest.mark.asyncio
async def test_a_failed_repair_keeps_the_plan_and_says_so():
    plan = _plan()
    record = await ensure_findings_covered(RepairLLM(raises=RuntimeError("overloaded")), plan,
                                           FOUND, summary="s")
    assert record["repair_error"] == "RuntimeError: overloaded"
    assert all(f["status"] == "unaccounted" for f in record["finding_outcomes"])
    assert len(plan.happy_path) == 1


def test_findings_carry_ids_and_the_accounting_rule_in_the_prompt():
    assert render_findings(FOUND).startswith("- X1 [guard]")
    from tests.test_bounce_in_prompts import _PromptOnlyClient
    dev = {"pull_requests": [], "commits": [], "branches": [], "code_exploration": FOUND}
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, dev)
    assert "exploration_coverage" in prompt
