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
    PER_EXPLORER,
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
    result = await explore(llm, gh, summary="Owner only", description="", pull_requests=PRS, explorers=("siblings",))
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
    await explore(llm, gh, summary="s", description="", pull_requests=PRS, explorers=("siblings",))
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
    result = await explore(llm, gh, summary="s", description="", pull_requests=PRS, explorers=("siblings",))
    assert gh.reads == [] and result["findings"] == []
    assert "outside the organizations" in llm.sent[-1][-1]["content"][0]["content"]


@pytest.mark.asyncio
async def test_a_throttled_search_is_not_no_matches():
    llm = ScriptedLLM([[call("search_code", "a", query="signerToken")],
                       [call("report_findings", "b", findings=[])]])
    await explore(llm, FakeGitHub(throttle=True), summary="s", description="", pull_requests=PRS,
                  explorers=("siblings",))
    told = llm.sent[-1][-1]["content"][0]["content"]
    assert "not 'no matches'" in told


@pytest.mark.asyncio
async def test_running_out_of_turns_is_reported():
    turns = PER_EXPLORER["max_turns"]
    llm = ScriptedLLM([[call("search_code", f"s{i}", query="x")] for i in range(turns + 1)])
    result = await explore(llm, FakeGitHub(), summary="s", description="", pull_requests=PRS,
                           explorers=("siblings",))
    assert result["error"] == "siblings explorer ran out of turns"
    # The last turn told it to stop and report.
    assert "Call report_findings now" in str(llm.sent[-1])


@pytest.mark.asyncio
async def test_a_model_failure_is_reported_not_raised():
    result = await explore(ScriptedLLM([], raises=RuntimeError("overloaded")), FakeGitHub(),
                           summary="s", description="", pull_requests=PRS, explorers=("siblings",))
    assert result["error"] == "siblings explorer: RuntimeError: overloaded" and result["findings"] == []


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
    result = await explore(llm, FakeGitHub(), summary="s", description="", pull_requests=PRS,
                           explorers=("siblings",))
    assert all(p["cache_control"] == {"type": "ephemeral"} for p in llm.payloads)
    assert result["usage"]["input_tokens"] == 200 and result["usage"]["cache_read_input_tokens"] == 100


# ── findings the plan dropped get cases from a separate call ──────────────────

from src.app.code_exploration import (  # noqa: E402
    MAX_ADDED_CASES,
    dropped_findings,
    finding_tokens,
    repair_dropped_findings,
)
from src.app.models import TestPlan  # noqa: E402


def _plan(**sections):
    return TestPlan(happy_path=sections.get("happy_path", []), edge_cases=sections.get("edge_cases", []),
                    regression_checklist=[], integration_tests=sections.get("integration_tests", []),
                    security_negative_tests=[], risks_and_gaps=sections.get("risks_and_gaps", []))


def _found(*specs):
    return {"findings": [{**finding("acme/ui", path, kind=kind), "what_it_does": what}
                         for path, kind, what in specs]}


MODAL = ("packages/signer-sdk/src/components/SignatureSettingsModal/index.tsx", "guard",
         "renders `in-person-identity-checkbox` before Sign is enabled")
UTILS = ("packages/sender/src/common/inPersonSigning/utils.ts", "guard", "keeps the active group only")
REACH = ("packages/sender/src/x/Unreached.tsx", "reachability", "nothing produces this error")


def test_tokens_name_the_component_not_a_generic_folder():
    assert finding_tokens({"path": MODAL[0], "what_it_does": MODAL[2]}) == [
        "SignatureSettingsModal/index.tsx", "SignatureSettingsModal", "in-person-identity-checkbox"]
    assert finding_tokens({"path": UTILS[0], "what_it_does": ""}) == ["inPersonSigning/utils.ts"]


def test_a_finding_the_plan_mentions_anywhere_is_not_dropped():
    plan = _plan(edge_cases=[{"title": "x", "expected_source": "packages/.../SignatureSettingsModal/index.tsx:150"}],
                 risks_and_gaps=[{"gap": "only `in-person-identity-checkbox`", "evidence": ""}])
    assert dropped_findings(plan, _found(MODAL, UTILS, REACH)) == [
        _found(UTILS)["findings"][0]]  # reachability is never repaired


class RepairLLM(ScriptedLLM):
    def __init__(self, cases=None, raises=None):
        super().__init__([], raises=raises)
        self.cases, self.calls = cases or [], 0

    async def _post_messages(self, payload, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        self.payload = payload
        return {"content": [{"type": "tool_use", "id": "r", "name": "add_cases",
                             "input": {"cases": self.cases}}]}


def _case(n, section="edge_cases", covers=("X2",)):
    return {"section": section, "covers_findings": list(covers), "title": f"case {n}", "steps": ["s"],
            "expected": "e", "priority": "medium", "surface": "web_ui", "credentials": "owner",
            "expected_verified": False}


@pytest.mark.asyncio
async def test_dropped_findings_get_cases_appended_and_recorded():
    plan = _plan(happy_path=[{"title": "owner hands off", "steps": ["SignatureSettingsModal shows"]}])
    llm = RepairLLM(cases=[_case(1)])
    record = await repair_dropped_findings(llm, plan, _found(MODAL, UTILS, REACH), summary="Owner only")
    assert plan.edge_cases[-1]["title"] == "case 1" and plan.edge_cases[-1]["added_for_findings"]
    assert "section" not in plan.edge_cases[-1]
    outcomes = {o["id"]: o["outcome"] for o in record["finding_outcomes"]}
    assert outcomes == {"X1": "used", "X2": "added:edge_cases:0", "X3": "skipped_reachability"}
    sent = llm.payload["messages"][0]["content"]
    assert "X2" in sent and "SignatureSettingsModal" not in sent and "owner hands off" in sent


@pytest.mark.asyncio
async def test_nothing_dropped_means_no_call():
    plan = _plan(happy_path=[{"title": "t", "steps": ["SignatureSettingsModal", "inPersonSigning/utils.ts"]}])
    llm = RepairLLM()
    record = await repair_dropped_findings(llm, plan, _found(MODAL, UTILS), summary="s")
    assert llm.calls == 0 and "cases_added" not in record


@pytest.mark.asyncio
async def test_added_cases_are_capped():
    plan = _plan()
    llm = RepairLLM(cases=[_case(i) for i in range(MAX_ADDED_CASES + 3)])
    await repair_dropped_findings(llm, plan, _found(UTILS), summary="s")
    assert len(plan.edge_cases) == MAX_ADDED_CASES


@pytest.mark.asyncio
async def test_a_failed_repair_keeps_the_plan_and_says_so():
    plan = _plan(happy_path=[{"title": "t"}])
    record = await repair_dropped_findings(RepairLLM(raises=RuntimeError("overloaded")), plan,
                                           _found(UTILS), summary="s")
    assert record["repair_error"] == "RuntimeError: overloaded"
    assert [o["outcome"] for o in record["finding_outcomes"]] == ["dropped"]
    assert plan.happy_path == [{"title": "t"}]


def test_the_main_plan_tool_stays_free_of_exploration_duties():
    """f02bd19 added an exploration accounting field to submit_test_plan and
    generation began failing as empty plans (3 of 8). Keep repair outside it."""
    from src.app.llm_client import SUBMIT_TEST_PLAN_TOOL, TEST_CASE_SCHEMA
    props = SUBMIT_TEST_PLAN_TOOL["input_schema"]["properties"]
    assert not [k for k in props if "exploration" in k or "finding" in k]
    assert "covers_findings" not in TEST_CASE_SCHEMA["properties"]


def test_repair_runs_before_the_critics_so_added_cases_are_checked():
    import inspect
    from src.app.services import plan_service
    src = inspect.getsource(plan_service.generate_single)
    assert src.index("repair_dropped_findings(") < src.index("run_grounding_critic(")
    assert src.index("repair_dropped_findings(") < src.index("quarantine_ungrounded_cases(")


# ── two focused explorers and the package map ─────────────────────────────────

from src.app.code_exploration import build_brief, package_map, package_of  # noqa: E402


def test_packages_are_monorepo_folders_or_top_level_projects():
    assert package_of("packages/signer-sdk/src/a.tsx") == "packages/signer-sdk"
    assert package_of("apps/web/src/a.tsx") == "apps/web"
    assert package_of("DigiSign.Sender.Envelope.Application/Controllers/X.cs") == \
        "DigiSign.Sender.Envelope.Application"
    assert package_of("README.md") is None and package_of(".github/workflows/ci.yml") is None


def test_package_map_marks_changed_and_lists_the_rest_without_tests():
    prs = [{"repository": "acme/ui", "files_changed": [{"filename": "packages/sender/src/a.ts"}]}]
    tree = ["packages/sender/src/a.ts", "packages/signer-sdk/src/Modal.tsx", "packages/e2e/x.ts",
            "docs/readme.md", "Api.Tests/T.cs", "package.json"]
    m = package_map(prs, {"acme/ui": tree})
    assert m == {"acme/ui": {"changed": ["packages/sender"], "others": ["packages/signer-sdk"]}}
    brief = build_brief("s", "", prs, packages=m)
    assert "acme/ui: *packages/sender, packages/signer-sdk" in brief


class RoutedLLM(ScriptedLLM):
    """Answers each explorer from its own script, picked by its system prompt."""

    def __init__(self, scripts, raises_for=None):
        super().__init__([])
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.raises_for = raises_for

    async def _post_messages(self, payload, **kw):
        system = payload["system"][0]["text"]
        name = "downstream" if "Downstream flows" in system else "siblings"
        if name == self.raises_for:
            raise RuntimeError("overloaded")
        turns = self.scripts.get(name) or []
        return {"content": turns.pop(0) if turns else [{"type": "text", "text": "done"}]}


class TreeGitHub(FakeGitHub):
    async def list_tree(self, repo, ref):
        return ["Api/StartInPersonSessionHandler.cs", "Signer/Modal.cs"]


@pytest.mark.asyncio
async def test_both_explorers_run_and_their_findings_merge_one_per_file():
    gh = TreeGitHub(files={"acme/envelope-api:Signer/Modal.cs": "x",
                           "acme/envelope-api:Api/Other.cs": "y"})
    llm = RoutedLLM({
        "siblings": [[call("read_file", "a", repo="acme/envelope-api", path="Api/Other.cs")],
                     [call("report_findings", "b", findings=[finding("acme/envelope-api", "Api/Other.cs")])]],
        "downstream": [[call("read_file", "c", repo="acme/envelope-api", path="Signer/Modal.cs"),
                        call("read_file", "d", repo="acme/envelope-api", path="Api/Other.cs")],
                       [call("report_findings", "e", findings=[
                           finding("acme/envelope-api", "Signer/Modal.cs", kind="guard"),
                           finding("acme/envelope-api", "Api/Other.cs")])]],
    })
    result = await explore(llm, gh, summary="s", description="", pull_requests=PRS)
    assert sorted(f["path"] for f in result["findings"]) == ["Api/Other.cs", "Signer/Modal.cs"]
    assert result["reads"] == 3 and result["error"] is None
    assert result["packages"] == {"acme/envelope-api": {"changed": ["Api"], "others": ["Signer"]}}


@pytest.mark.asyncio
async def test_one_explorer_failing_keeps_the_others_findings_and_says_so():
    gh = TreeGitHub(files={"acme/envelope-api:Api/Other.cs": "y"})
    llm = RoutedLLM({"siblings": [[call("read_file", "a", repo="acme/envelope-api", path="Api/Other.cs")],
                                  [call("report_findings", "b", findings=[finding("acme/envelope-api", "Api/Other.cs")])]]},
                    raises_for="downstream")
    result = await explore(llm, gh, summary="s", description="", pull_requests=PRS)
    assert [f["path"] for f in result["findings"]] == ["Api/Other.cs"]
    assert result["error"] == "downstream explorer: RuntimeError: overloaded"


# ── replay: reads outside the PRs are pinned to the bounce cutoff ─────────────

from src.app.code_exploration import Workspace  # noqa: E402


class PinnedGitHub(FakeGitHub):
    def __init__(self, sha="old1234", **kw):
        super().__init__(**kw)
        self.sha, self.asked = sha, []

    async def commit_before(self, repo, iso):
        self.asked.append((repo, iso))
        return self.sha


@pytest.mark.asyncio
async def test_a_pinned_read_uses_the_commit_before_the_cutoff():
    gh = PinnedGitHub(files={"acme/envelope-api:Api/Other.cs": "x",
                             "acme/envelope-api:Api/StartInPersonSessionHandler.cs": "y"})
    ws = Workspace(gh, PRS, as_of="2026-03-02T09:34:00+00:00")
    out = await ws.read_file("acme/envelope-api", "Api/Other.cs")
    assert ("acme/envelope-api", "old1234", "Api/Other.cs") in gh.reads and "as of 2026-03-02" in out
    # A PR's own file is still read at the PR's commit, not the pin.
    await ws.read_file("acme/envelope-api", "Api/StartInPersonSessionHandler.cs")
    assert ("acme/envelope-api", "6d9a355abc", "Api/StartInPersonSessionHandler.cs") in gh.reads
    assert len(gh.asked) == 1  # one lookup per repo


@pytest.mark.asyncio
async def test_no_commit_before_the_cutoff_means_no_read_never_todays_code():
    gh = PinnedGitHub(sha=None, files={"acme/envelope-api:Api/Other.cs": "today's fix"})
    ws = Workspace(gh, PRS, as_of="2026-03-02T09:34:00+00:00")
    out = await ws.read_file("acme/envelope-api", "Api/Other.cs")
    assert gh.reads == [] and "was not read" in out
    assert ("acme/envelope-api", "Api/Other.cs") not in ws.read_files
