"""Code claims a plan marks as grounded are re-read before it is saved.

A UAT run on SK-2719 (plan 1 v1) found three claims the plan marked as
grounded in code that the code does not make:

1. Steps tapped 'Sign In Person', citing FromActionItem.swift, whose literal is
   "Sign In-Person" (line 316 at ios-client 50aff26).
2. integration_tests:1 and edge_cases:1 restated the picker rule from its guard
   clause and were verified against ``activeSigningRecipients``, which also
   requires ``status == "InProgress"`` (line 60) and keeps only the lowest
   ``group.order`` (lines 63-67).
3. integration_tests:1 said "Bearer <agent token>" for a call the iOS app makes
   with scopes the runner's contract-test token lacks; the downstream said 401.

The root cause was what the generator was shown: each iOS file's diff was cut
to 600 characters, so neither the label nor the rest of the function reached
it. ``source_facts`` now reads the changed files whole and gives the prompt the
UI strings, the whole rule functions with their conditions, and the client's
token scopes; ``grounding_verification`` re-reads every claim before save.

tests/fixtures/sk2719/sources.json holds sanitized excerpts of the cited files
at their real line numbers; tests/fixtures/grounding_web is a synthetic
React/TypeScript + FastAPI ticket with the same three mistakes, to show none of
this is iOS-specific.
"""

from __future__ import annotations

import copy
import json
import pathlib
from unittest.mock import patch

import pytest

from src.app import grounding_verification as gv
from src.app import source_facts as sf
from src.app.models import TestPlan

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
IOS = "acme/ios-client"
VM = "Acme/Classes/ViewControllers/EnvelopesModule/InPersonSigning/InPersonSigningViewModel.swift"
MENU = "Acme/Classes/ViewControllers/TransactionModule/TransactionDetails/BaseController/Models/FromActionItem.swift"
CONTRACT_TOKEN = [{"name": "contract-test", "scopes": ["com.acme.forms.api"]}]


def _expand(f) -> str:
    if isinstance(f, str):
        return f
    return "\n" * (f["pad_to"] - 1) + f["text"]


class FakeGitHub:
    """Trees and files at each PR's head; a path it lacks is not found, and
    ``fail`` makes a read raise, like GitHub timing out."""

    def __init__(self, trees: dict, files: dict, fail: set[str] | None = None):
        self.trees, self.files, self.fail = trees, files, fail or set()

    async def list_tree(self, repo, ref):
        tree = self.trees.get(repo)
        return list(tree) if tree is not None else None

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        if any(p in self.fail for p in paths):
            raise TimeoutError("GitHub timed out")
        files = self.files.get(repo, {})
        return {p: _expand(files[p]) for p in paths if p in files}


def _sk2719():
    inputs = json.loads((FIXTURES / "sk2719" / "inputs.json").read_text())
    plan = json.loads((FIXTURES / "sk2719" / "plan_v1.json").read_text())
    sources = json.loads((FIXTURES / "sk2719" / "sources.json").read_text())
    prs = copy.deepcopy(inputs["pull_requests"])
    for pr in prs:
        for f in pr["files_changed"]:
            patch_text = sources["patches"].get(pr["repository"], {}).get(f["filename"])
            if patch_text:
                f["patch"] = patch_text
    trees = {r: list(t) for r, t in inputs["repo_trees"].items()}
    trees["acme/acme-deployments"] = list(inputs["deployments_tree"])
    files: dict = {}
    for repo, fs in sources["files"].items():
        files.setdefault(repo, {}).update(fs)
        trees.setdefault(repo, [])
        trees[repo] += [p for p in fs if p not in trees[repo]]
    plan["edge_cases"] = sources["edge_cases"]
    return prs, FakeGitHub(trees, files), plan


def _web():
    d = json.loads((FIXTURES / "grounding_web" / "inputs.json").read_text())
    return copy.deepcopy(d["pull_requests"]), FakeGitHub(d["repo_trees"], d["files"]), d["plan"]


def _test_plan(body: dict) -> TestPlan:
    b = copy.deepcopy(body)
    return TestPlan(happy_path=b.get("happy_path") or [], edge_cases=b.get("edge_cases") or [],
                    regression_checklist=b.get("regression_checklist") or [],
                    integration_tests=b.get("integration_tests") or [],
                    security_negative_tests=b.get("security_negative_tests") or [], risks_and_gaps=[])


async def _index_and_facts(prs, gh, token_sources=None):
    index = await sf.read_source_index(gh, prs)
    return index, sf.build_source_facts(index, token_sources)


def _case(plan: TestPlan, case_id: str) -> dict:
    section, i = case_id.split(":")
    return getattr(plan, section)[int(i)]


# ── 1. labels come from the code ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_generator_is_shown_the_code_literal_for_the_more_menu_row():
    prs, gh, _ = _sk2719()
    _, facts = await _index_and_facts(prs, gh)
    [row] = [u for u in facts["ui_strings"] if u["key"] == "TransactionAction.signInPersonButtonTitle"]
    assert (row["text"], row["path"], row["line"]) == ("Sign In-Person", MENU, 316)
    assert f'"Sign In-Person" — {MENU}:316' in sf.render_source_facts(facts)


@pytest.mark.asyncio
async def test_ticket_wording_for_a_label_is_downgraded_with_the_code_literal_named():
    prs, gh, body = _sk2719()
    index, facts = await _index_and_facts(prs, gh)
    plan = _test_plan(body)
    steps_before = [len(c["steps"]) for c in plan.happy_path + plan.edge_cases + plan.integration_tests]

    report = gv.verify_plan(plan, index, shown=gv.shown_lines([{"pull_requests": prs}], facts=facts))

    happy = _case(plan, "happy_path:0")
    marked = [s for s in happy["steps"] if s.startswith(gv.LABEL_MARKER)]
    assert marked and all("“Sign In Person”; the cited code has “Sign In-Person” at FromActionItem.swift:316" in s
                          for s in marked)
    assert {"label": "Sign In Person", "code_has": "Sign In-Person"}.items() <= happy["unverified_labels"][0].items()
    assert report["by_kind"]["label"]["downgraded"] >= 3
    # Marks in place: no case or step added, removed or reordered.
    assert [len(c["steps"]) for c in plan.happy_path + plan.edge_cases + plan.integration_tests] == steps_before


@pytest.mark.asyncio
async def test_a_step_that_quotes_the_code_literal_is_verified_against_its_line():
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    fixed = json.loads(json.dumps(body["happy_path"][0]).replace("Sign In Person", "Sign In-Person"))
    plan = _test_plan({"happy_path": [fixed]})

    report = gv.verify_plan(plan, index)

    labels = [c for c in report["downgraded_claims"] if c["kind"] == "label"]
    assert labels == []
    assert not any(s.startswith(gv.LABEL_MARKER) for s in plan.happy_path[0]["steps"])
    assert report["by_kind"]["label"]["verified"] >= 2


@pytest.mark.asyncio
async def test_a_label_looked_up_by_i18n_key_is_resolved_from_the_locale_file():
    """Synthetic web fixture: the label is t("orders.requestRefund") → en.json."""
    prs, gh, body = _web()
    index, facts = await _index_and_facts(prs, gh)
    assert any(u["text"] == "Request a refund" and u["path"] == "src/locales/en.json" for u in facts["ui_strings"])

    plan = _test_plan(body)
    gv.verify_plan(plan, index)
    step = plan.happy_path[0]["steps"][1]
    assert step.startswith(gv.LABEL_MARKER)
    assert "“Request a Refund”; the cited code has “Request a refund” at en.json:" in step

    fixed = _test_plan(json.loads(json.dumps(body).replace("Request a Refund", "Request a refund")))
    gv.verify_plan(fixed, index)
    assert not fixed.happy_path[0]["steps"][1].startswith(gv.LABEL_MARKER)


# ── 2. a rule is restated whole ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_generator_is_shown_the_whole_picker_rule_with_its_conditions():
    prs, gh, _ = _sk2719()
    _, facts = await _index_and_facts(prs, gh)
    [rule] = [f for f in facts["functions"] if f["name"] == "activeSigningRecipients"]
    assert (rule["path"], rule["start"], rule["end"]) == (VM, 49, 68)
    conditions = {c["condition"]: c["line"] for c in rule["conditions"]}
    assert conditions['status matches "InProgress"'] == 60
    assert conditions['role matches "Signer"'] == 59
    assert conditions["keeps the min group.order"] == 63
    text = sf.render_source_facts(facts)
    assert '   60| \t\t\t\t&& status.caseInsensitiveCompare("InProgress") == .orderedSame' in text
    assert "   67| \t\treturn candidates.filter { $0.group?.order == activeGroupOrder }" in text
    assert [49, 68] in facts["shown"][VM]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ["integration_tests:1", "edge_cases:1"])
async def test_a_rule_summarised_from_its_guard_clause_is_downgraded(case_id):
    prs, gh, body = _sk2719()
    index, facts = await _index_and_facts(prs, gh)
    plan = _test_plan(body)

    gv.verify_plan(plan, index, shown=gv.shown_lines([{"pull_requests": prs}], facts=facts))

    case = _case(plan, case_id)
    assert case["expected_verified"] is False
    why = case["expected_verification_downgraded"]
    assert 'status matches "InProgress" (line 60)' in why
    assert "keeps the min group.order (line 63)" in why
    assert "activeSigningRecipients" in why


@pytest.mark.asyncio
async def test_a_rule_restated_whole_and_cited_by_range_stays_verified():
    prs, gh, body = _sk2719()
    index, facts = await _index_and_facts(prs, gh)
    case = copy.deepcopy(body["integration_tests"][1])
    case["steps"][2] = (
        "Identify which recipients satisfy the iOS eligibility rule (activeSigningRecipients): status "
        "non-null AND status equal to 'InProgress' (case-insensitive) AND isActive == true AND group "
        "non-null AND role equal to 'Signer' (case-insensitive); then keep only those whose group.order "
        "is the lowest among them.")
    case["expected_source"] = f"{VM}:38-68"
    plan = _test_plan({"integration_tests": [case]})

    report = gv.verify_plan(plan, index, shown=gv.shown_lines([{"pull_requests": prs}], facts=facts),
                            token_sources=CONTRACT_TOKEN)

    assert plan.integration_tests[0]["expected_verified"] is True, report["downgraded_claims"]


@pytest.mark.asyncio
async def test_a_complete_rule_the_generator_was_not_shown_is_still_downgraded():
    """The pre-fix prompt: 600 characters of the new file's diff, no facts."""
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    case = copy.deepcopy(body["integration_tests"][1])
    case["steps"][2] = ("Keep recipients with status 'InProgress', isActive, a group, role 'Signer', in the "
                        "lowest group.order.")
    plan = _test_plan({"integration_tests": [case]})

    gv.verify_plan(plan, index, shown={VM: set(range(1, 20))})

    why = plan.integration_tests[0]["expected_verification_downgraded"]
    assert "the generator was not shown InPersonSigningViewModel.swift lines" in why
    assert "the rule spans code it did not read" in why


@pytest.mark.asyncio
async def test_a_partial_rule_is_downgraded_in_typescript_too():
    """Synthetic web fixture: status and refundedAt, but not isGift, the
    30-day window in the helper, or the keep-the-newest step."""
    prs, gh, body = _web()
    index, facts = await _index_and_facts(prs, gh)
    plan = _test_plan(body)

    gv.verify_plan(plan, index, shown=gv.shown_lines([{"pull_requests": prs}], facts=facts))

    why = plan.happy_path[0]["expected_verification_downgraded"]
    assert "isGift === false" in why
    assert "ageDays <= REFUND_WINDOW_DAYS (line 7)" in why
    assert "keeps the max deliveredAt (line 17)" in why
    # The backend case cites the handler line that returns its 409.
    assert plan.integration_tests[0]["expected_verified"] is True


# ── 3. a backend case that mirrors a client call names the client's token ────


@pytest.mark.asyncio
async def test_the_ios_apps_token_scopes_are_stated_and_the_case_is_blocked_without_one():
    prs, gh, body = _sk2719()
    index, facts = await _index_and_facts(prs, gh, CONTRACT_TOKEN)
    assert "com.acme.signsvc.userid" in sf.render_source_facts(facts)
    plan = _test_plan(body)

    report = gv.verify_plan(plan, index, token_sources=CONTRACT_TOKEN)

    case = _case(plan, "integration_tests:1")
    token = case["client_token"]
    assert token["source"] == "Acme/Application/Okta/Okta-Integ.plist:14"
    assert {"com.acme.forms.api", "com.acme.signsvc.userid"} <= set(token["required_scopes"])
    assert "openid" not in token["required_scopes"]
    assert case["needs_client_equivalent_token"] is True
    assert "com.acme.signsvc.userid" in case["needs_client_equivalent_token_reason"]
    assert "BLOCKED" in case["needs_client_equivalent_token_reason"]
    assert "integration_tests:1" in report["needs_client_equivalent_token"]
    assert any(c["case_id"] == "integration_tests:1" and c["kind"] == "client_token"
               for c in report["downgraded_claims"])


@pytest.mark.asyncio
async def test_a_runner_token_with_the_clients_scopes_unblocks_the_case():
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    plan = _test_plan(body)
    okta = sf.parse_auth_config("x.plist", _expand(gh.files[IOS]["Acme/Application/Okta/Okta-Integ.plist"]))
    able = [{"name": "agent-user-token", "scopes": okta["scopes"]}]

    gv.verify_plan(plan, index, token_sources=able)

    case = _case(plan, "integration_tests:1")
    assert not case.get("needs_client_equivalent_token")
    assert case["client_token"]["runner_source"] == "agent-user-token"


@pytest.mark.asyncio
async def test_credentials_that_state_the_scopes_and_cite_the_config_verify():
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    case = copy.deepcopy(body["integration_tests"][1])
    scopes = sf.api_scopes(sf.parse_auth_config(
        "x.plist", _expand(gh.files[IOS]["Acme/Application/Okta/Okta-Integ.plist"]))["scopes"])
    case["credentials"] = f"User token with the iOS app's scopes {' '.join(scopes)} (Okta-Integ.plist:14)."
    plan = _test_plan({"integration_tests": [case]})

    report = gv.verify_plan(plan, index, token_sources=CONTRACT_TOKEN)

    assert report["by_kind"]["client_token"] == {"verified": 1, "downgraded": 0, "unchecked": 0}
    assert plan.integration_tests[0]["needs_client_equivalent_token"] is True  # still no such runner token


@pytest.mark.asyncio
async def test_a_backend_case_mirroring_a_web_call_gets_the_web_apps_oidc_scopes():
    prs, gh, body = _web()
    index, _ = await _index_and_facts(prs, gh)
    plan = _test_plan(body)

    gv.verify_plan(plan, index, token_sources=[{"name": "service", "scopes": ["orders:read"]}])

    case = plan.integration_tests[0]
    assert case["client_token"]["source"] == "src/auth/oidcConfig.ts:5"
    assert case["client_token"]["required_scopes"] == ["orders:read", "refunds:write"]
    assert case["client_token"]["audience"] == "api://shop"
    assert case["needs_client_equivalent_token"] is True


# ── the pass itself ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_file_that_could_not_be_read_is_unchecked_not_downgraded():
    """A read that failed is not evidence the label is absent."""
    prs, gh, body = _sk2719()
    gh.fail = {MENU}
    index, _ = await _index_and_facts(prs, gh)
    assert any(p == MENU for _, p in index.unreadable)
    case = copy.deepcopy(body["edge_cases"][1])
    case["grounded_in"] = [f"pr:{MENU}"]
    case["step_grounding"] = [e for e in case["step_grounding"] if MENU in e["source"]]
    plan = _test_plan({"edge_cases": [case]})

    report = gv.verify_plan(plan, index)

    assert report["by_kind"]["label"]["unchecked"] >= 1
    assert report["by_kind"]["label"]["downgraded"] == 0
    assert not any(s.startswith(gv.LABEL_MARKER) for s in plan.edge_cases[0]["steps"])


@pytest.mark.asyncio
async def test_a_flag_named_in_the_wrong_file_is_downgraded_with_where_it_is():
    """The stored happy_path:0 cited FeatureManager.swift for the Remote Config
    keys; the strings live in RemoteConfigKey.swift, which edge_cases:0 cites."""
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    wrong = copy.deepcopy(body["edge_cases"][0])
    for e in wrong["step_grounding"]:
        if e["kind"] == "flag":
            e["source"] = "pr:Acme/Services/FeatureManager.swift"
    plan = _test_plan({"edge_cases": [body["edge_cases"][0], wrong]})

    gv.verify_plan(plan, index)

    assert not [d for d in plan.edge_cases[0].get("grounding_downgrades") or [] if d["kind"] == "element"]
    [d] = [d for d in plan.edge_cases[1]["grounding_downgrades"] if d["kind"] == "element"]
    assert d["reason"] == "sign_in_person, sign_now not in FeatureManager.swift (found in RemoteConfigKey.swift)"
    assert plan.edge_cases[1]["steps"][0].startswith(gv.ELEMENT_MARKER)


@pytest.mark.asyncio
async def test_the_report_counts_downgrades_for_the_plan_metadata():
    prs, gh, body = _sk2719()
    index, facts = await _index_and_facts(prs, gh, CONTRACT_TOKEN)
    plan = _test_plan(body)

    report = await gv.run_grounding_verification(
        plan, index, gh, shown=gv.shown_lines([{"pull_requests": prs}], facts=facts), token_sources=CONTRACT_TOKEN)

    assert report["downgraded"] == len(report["downgraded_claims"]) > 0
    assert report["checked"] >= report["downgraded"]
    assert sum(k["downgraded"] for k in report["by_kind"].values()) == report["downgraded"]
    assert report["ids_are"] == "raw body positions before quarantine"


@pytest.mark.asyncio
async def test_generation_records_the_reread_and_keeps_case_ids():
    """The SK-2719 fixture through generate_single with a stub model."""
    from tests.test_sk2719_sk2889_regressions import SK2719, _replay
    from src.app.services import plan_service

    prs, gh, body = _sk2719()
    plan_fixture = json.loads((FIXTURES / "sk2719" / "plan_v1.json").read_text())

    async def _sources(dev_info):
        return await sf.read_source_index(gh, [p for p in prs if p["repository"] in
                                               {x.get("repository") for x in dev_info["pull_requests"]}])

    with (patch.object(plan_service, "_read_sources", _sources),
          patch.object(plan_service.settings, "runner_token_sources", CONTRACT_TOKEN)):
        response = await _replay(SK2719, plan_fixture, "SK-2719")

    report = response["source_provenance"]["grounding_verification"]
    assert report["downgraded"] > 0
    assert response["source_provenance"]["source_facts"]["rule_functions"] >= 1
    sections = {s: len(response[s]) for s in ("happy_path", "integration_tests")}
    assert sections["happy_path"] == len(plan_fixture["happy_path"])
    recipients = next(c for c in response["integration_tests"] if c["title"].startswith("GET /envelopes/{id}/recipients"))
    assert recipients["expected_verified"] is False
    assert recipients["needs_client_equivalent_token"] is True


@pytest.mark.asyncio
async def test_a_cited_file_outside_the_prs_is_read_from_the_tree_or_reported_unreadable():
    """A file only an exploration finding names. The tree decides whether it
    exists; a fetch that comes back empty is a failed read, not an absence."""
    prs, gh, body = _sk2719()
    index, _ = await _index_and_facts(prs, gh)
    absent = copy.deepcopy(body["integration_tests"][1])
    absent["expected_source"] = "Acme/Nowhere/Missing.swift:10"
    in_tree = copy.deepcopy(body["integration_tests"][1])
    in_tree["expected_source"] = "Acme/Services/Routing/SSRouter.swift:10"  # in the tree, not in the fixture files
    plan = _test_plan({"integration_tests": [absent, in_tree]})

    report = await gv.run_grounding_verification(plan, index, gh, shown=None, token_sources=CONTRACT_TOKEN)

    assert "not a file in the linked PRs' repos" in plan.integration_tests[0]["expected_verification_downgraded"]
    assert plan.integration_tests[1]["expected_verified"] is True
    assert any(c["case_id"] == "integration_tests:1" and c["kind"] == "expected" for c in report["unchecked_claims"])


# ── extractors on other languages ────────────────────────────────────────────


def test_android_strings_xml_and_kotlin_references_resolve():
    xml = '<resources>\n  <string name="sign_in_person">Sign In-Person</string>\n</resources>\n'
    kt = 'binding.action.text = getString(R.string.sign_in_person)\n'
    res = sf.resource_strings("app/src/main/res/values/strings.xml", xml)
    assert [(r.key, r.text, r.line) for r in res] == [("sign_in_person", "Sign In-Person", 2)]
    assert [r.text for r in sf.resolve_keys(sf.referenced_keys(kt), res)] == ["Sign In-Person"]


def test_kotlin_and_python_rules_yield_their_conditions():
    kt = (
        "fun eligible(items: List<Item>): List<Item> {\n"
        "    val open = items.filter { it.state == \"Open\" && it.owner != null }\n"
        "    val oldest = open.minOf { it.createdAt }\n"
        "    return open.filter { it.createdAt == oldest }\n"
        "}\n")
    fn = sf.function_named(sf.functions("E.kt", kt), "eligible")
    kinds = {(a.kind, a.literal or a.op) for a in sf.condition_atoms("E.kt", fn.body()[1:])}
    assert {("literal", "Open"), ("nonnull", "nonnull"), ("aggregate", "min")} <= kinds

    py = (
        "def eligible(items):\n"
        "    out = [i for i in items if i.state == 'Open' and i.owner is not None]\n"
        "    return out\n"
        "\n"
        "def other():\n"
        "    pass\n")
    fns = sf.functions("e.py", py)
    fn = sf.function_named(fns, "eligible")
    assert (fn.start, fn.end) == (1, 3)
    kinds = {(a.kind, a.literal or a.op) for a in sf.condition_atoms("e.py", fn.body()[1:])}
    assert {("literal", "Open"), ("nonnull", "nonnull")} <= kinds


def test_a_format_string_matches_a_label_only_when_it_supplies_the_words():
    lits = [sf.UIString("%@ is signing", "W.swift", 10), sf.UIString(" %@", "E.swift", 186)]
    assert sf.find_literal("[First Last] is signing", lits).line == 10
    assert sf.find_literal("Sign In Person", lits) is None


def test_labels_are_read_only_from_ui_context():
    assert sf.quoted_labels("Tap 'Save' and type 'Jane' into the Name field.") == ["Save"]
    assert sf.quoted_labels("more menu → 'Signers'") == ["Signers"]
    assert sf.quoted_labels("confirm a 'Sign In Person' row is present") == ["Sign In Person"]
    assert sf.quoted_labels("the ticket calls it 'in-person signing'") == []
