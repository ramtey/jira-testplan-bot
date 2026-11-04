"""Rule P: a case's steps must be performable on the surface it names.

The regression is SK-2948 (plan 655, sanitized here). Two mobile cases said
"leave the city blank" to reach the server's formula path. The mobile app
takes an address only as a full street address picked from autocomplete, so
the step could never be performed. Only the web app has a separate City field.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app import surface_inputs as si
from src.app.case_disposition import CODE_REVIEW_KEY, route_out_of_uat
from src.app.llm_client import SYSTEM_PROMPT, TEST_CASE_SCHEMA

MOBILE_REPO = "acme/calculator-mobile"
WEB_REPO = "acme/calculator-web"
MOBILE_ADDRESS = "src/screens/PropertyAddress.tsx"
WEB_FORM = "src/components/PropertyForm.tsx"
FILES = {
    (MOBILE_REPO, MOBILE_ADDRESS): (
        "export function PropertyAddress() {\n"
        "  // Only a full street address picked from the autocomplete list is accepted.\n"
        "  return <AddressAutocomplete onSelect={setPlace} requireStreetAddress />;\n"
        "}\n"
    ),
    (WEB_REPO, WEB_FORM): (
        "<StateSelect name=\"state\" />\n<CountySelect name=\"county\" />\n"
        "<TextField name=\"city\" label=\"City\" required={false} />\n"
    ),
}
PRS = [{"repository": "acme/calculator", "number": 1387, "title": "Seller CPL fix", "head_sha": "7de5c09",
        "files_changed": [{"filename": "apps/server/src/fee-formula-service.ts",
                           "patch": "@@\n+if (!city) return buildFromFormula(county)"}]}]
DEV = [{"pull_requests": PRS}]

COUNTY_ONLY_STEPS = [
    "Create a seller calculation supplying only state and county (leave the city blank) "
    "for a county that has a stored GFE formula.",
    "Let the fees populate and open the Closing Service Fees list on the seller results.",
    "Confirm the fee source metadata shows the formula path rather than a live GFE quote.",
    "List every Closing Protection Letter line present.",
]


def county_only_case(**kw):
    return {"title": "County-only quote (no city): seller sheet from stored formulas shows no buyer or lender CPL",
            "priority": "high", "surface": "mobile", "credentials": "test agent",
            "steps": list(COUNTY_ONLY_STEPS),
            "expected": "No buyer or lender Closing Protection Letter line appears.",
            "expected_verified": True, **kw}


def plan(*happy, **sections):
    base = {"happy_path": list(happy), "edge_cases": [], "integration_tests": [],
            "security_negative_tests": [], "regression_checklist": [], "risks_and_gaps": []}
    base.update(sections)
    return SimpleNamespace(**base)


class FakeGitHub:
    async def search_code_hits(self, query, scope):
        return [{"repo": MOBILE_REPO, "path": MOBILE_ADDRESS, "fragments": ["AddressAutocomplete"]}]

    async def fetch_files_at_ref(self, repo, ref, paths):
        return {p: FILES[(repo, p)] for p in paths if (repo, p) in FILES}


class ScriptedLLM:
    """Reads the files it is told to, then reports the given verdicts."""

    model = "test"

    def __init__(self, verdicts, reads=((MOBILE_REPO, MOBILE_ADDRESS), (WEB_REPO, WEB_FORM)), error=None):
        self.verdicts, self.reads, self.error, self.turn = verdicts, reads, error, 0

    def _budget(self, *_a, **_k):
        return 4096

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        if self.error:
            raise self.error
        self.turn += 1
        if self.turn == 1 and self.reads:
            return {"content": [{"type": "tool_use", "id": f"r{i}", "name": "read_file",
                                 "input": {"repo": r, "path": p}} for i, (r, p) in enumerate(self.reads)]}
        return {"content": [{"type": "tool_use", "id": "rep", "name": "report_surface_inputs",
                             "input": {"verdicts": self.verdicts}}]}


MOBILE_LIMIT = {"step": 1, "kind": "action", "input": "property address (autocomplete)",
                "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3", "performable": False,
                "why": "the mobile app only accepts a full street address picked from autocomplete, so the city cannot be left blank"}
WEB_ALT = {
    "surface": "web_ui", "client": "the web app (acme/calculator-web)",
    "steps": ["In the web app's property form, choose Utah and Davis County and leave City empty.",
              "Let the fees populate and open the Closing Service Fees list on the seller results.",
              "Confirm the fee source metadata shows the formula path rather than a live GFE quote.",
              "List every Closing Protection Letter line present."],
    "inputs": [{"step": 1, "input": "City (optional TextField), County select", "source": f"{WEB_REPO}:{WEB_FORM}:2-3"}],
}
NOT_PERFORMABLE = {
    "id": "happy_path:0", "verdict": "not_performable", "client": "the mobile app (acme/calculator-mobile)",
    "reason": "no city-less address entry on mobile",
    "server_branch": "apps/server/src/fee-formula-service.ts:2 runs the formula path when no city is sent",
    "clients": [{"surface": "mobile", "client": "mobile app", "can_trigger": False,
                 "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3"},
                {"surface": "web_ui", "client": "web app", "can_trigger": True, "how": "leave City empty",
                 "source": f"{WEB_REPO}:{WEB_FORM}:3"}],
    "steps": [MOBILE_LIMIT, {"step": 2, "kind": "observe", "performable": True}],
    "alternatives": [WEB_ALT],
}


def _no_unperformable_mobile_city_step(test_plan) -> bool:
    for section in ("happy_path", "edge_cases", "integration_tests"):
        for case in getattr(test_plan, section):
            for step in case.get("steps") or []:
                if case.get("surface") == "mobile" and "leave the city blank" in step \
                        and not step.startswith(("Not performable", "Unverified")):
                    return False
    return True


# --- the SK-2948 regression --------------------------------------------------

@pytest.mark.asyncio
async def test_sk2948_county_only_case_is_not_kept_as_mobile_with_leave_city_blank():
    p = plan(county_only_case())
    changes, gap = await si.run_surface_input_critic(ScriptedLLM([NOT_PERFORMABLE]), FakeGitHub(), p, DEV)
    assert gap is None
    assert changes["rewritten"] == ["happy_path:0"]
    assert _no_unperformable_mobile_city_step(p)
    case = p.happy_path[0]
    assert case["surface"] == "web_ui"
    assert case["steps"][0].startswith("In the web app's property form")
    # The behaviour under test is unchanged; only how the tester reaches it.
    assert case["expected"] == "No buyer or lender Closing Protection Letter line appears."
    # Not silently relabelled: the old surface, steps and reason travel with the case.
    check = case["surface_check"]
    assert check["status"] == "rewritten"
    assert check["original_surface"] == "mobile"
    assert check["original_steps"] == COUNTY_ONLY_STEPS
    assert "autocomplete" in check["not_performable_steps"][0]["why"]
    assert any(c["surface"] == "web_ui" and c["can_trigger"] for c in check["clients"])
    assert case["surface_inputs"][0]["source"] == f"{WEB_REPO}:{WEB_FORM}:2-3"


@pytest.mark.asyncio
async def test_a_case_that_works_on_two_surfaces_is_split_one_per_surface():
    mobile_route = {
        "surface": "mobile", "client": "the mobile app (acme/calculator-mobile)",
        "steps": ["Set the service area to Davis County, Utah in Profile.",
                  "Start a seller calculation and leave the property address empty.",
                  "Open the Closing Service Fees list.", "List every Closing Protection Letter line present."],
        "inputs": [{"step": 1, "input": "Profile service area", "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:1"},
                   {"step": 2, "input": "address left empty → profile county fallback",
                    "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:2"}],
    }
    verdict = {**NOT_PERFORMABLE, "alternatives": [WEB_ALT, mobile_route]}
    other = {"title": "Same Utah twice", "surface": "backend_http", "steps": ["POST /quote"], "expected": "ok"}
    p = plan(county_only_case(), other)
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    assert changes["split"] == ["happy_path:0"]
    assert [c["surface"] for c in p.happy_path] == ["web_ui", "mobile", "backend_http"]
    assert p.happy_path[0]["title"].endswith("the web app (acme/calculator-web)")
    assert p.happy_path[1]["steps"][1].startswith("Start a seller calculation and leave the property address empty")
    assert all(c["surface_check"]["split_count"] == 2 for c in p.happy_path[:2])
    assert _no_unperformable_mobile_city_step(p)


@pytest.mark.asyncio
async def test_no_surface_can_do_it_so_it_leaves_the_checklist_with_the_reason():
    verdict = {**NOT_PERFORMABLE, "alternatives": [],
               "clients": [{**c, "can_trigger": False} for c in NOT_PERFORMABLE["clients"]],
               "suggested_test": "fee-formula-service.test.ts: build a request with no city, assert formula path"}
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    assert changes["routed_out"] == ["happy_path:0"]
    moved = route_out_of_uat(p)
    assert p.happy_path == []
    entry = moved[CODE_REVIEW_KEY][0]
    assert "No surface can perform this case (rule P)" in entry["why_not_uat"]
    assert "no city" in entry["suggested_test"]


@pytest.mark.asyncio
async def test_backend_http_is_an_allowed_replacement():
    alt = {"surface": "backend_http", "client": "the quote API",
           "steps": ["POST /api/fees with state UT, county Davis and no city.", "Read closingServiceFees."],
           "inputs": [{"step": 1, "input": "request body without city", "source": f"{WEB_REPO}:{WEB_FORM}:3"}]}
    p = plan(county_only_case(needs_manual_verification=True))
    await si.run_surface_input_critic(ScriptedLLM([{**NOT_PERFORMABLE, "alternatives": [alt]}]),
                                      FakeGitHub(), p, DEV)
    assert p.happy_path[0]["surface"] == "backend_http"
    assert "needs_manual_verification" not in p.happy_path[0]


# --- code decides what a citation is worth -----------------------------------

@pytest.mark.asyncio
async def test_not_performable_citing_a_file_never_read_marks_but_does_not_rewrite():
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([NOT_PERFORMABLE], reads=()), FakeGitHub(), p, DEV)
    case = p.happy_path[0]
    assert case["surface"] == "mobile" and not changes["rewritten"]
    assert case["steps"][0].startswith("Unverified — the surface check says")
    assert case["surface_check"]["status"] == "partly_checked"
    assert _no_unperformable_mobile_city_step(p)


@pytest.mark.asyncio
async def test_an_alternative_whose_inputs_were_not_read_is_not_used():
    alt = {**WEB_ALT, "inputs": [{"step": 1, "input": "City", "source": "acme/calculator-web:src/Other.tsx:9"}]}
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(
        ScriptedLLM([{**NOT_PERFORMABLE, "alternatives": [alt]}]), FakeGitHub(), p, DEV)
    # Not used as a rewrite; the web route in `clients` (read) keeps the case, marked.
    assert not changes["rewritten"] and changes["marked"] == ["happy_path:0"]
    assert p.happy_path[0]["surface"] == "mobile"
    assert "Use instead, on web app: leave City empty" in p.happy_path[0]["steps"][0]


@pytest.mark.asyncio
async def test_performable_needs_every_action_step_backed_by_a_read_file():
    case = {"title": "Utah seller sheet", "surface": "mobile", "steps": [
        "Pick a Layton, UT street address from the autocomplete list.",
        "Toggle Seller on the dashboard.",
        "Open the results."], "expected": "x"}
    verdict = {"id": "happy_path:0", "verdict": "performable", "client": "the mobile app", "reason": "ok",
               "steps": [{"step": 1, "kind": "action", "input": "AddressAutocomplete", "performable": True,
                          "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3"},
                         {"step": 2, "kind": "action", "input": "Seller toggle", "performable": True,
                          "source": f"{MOBILE_REPO}:src/Dashboard.tsx:10"},
                         {"step": 3, "kind": "observe", "performable": True}]}
    p = plan(case)
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    steps = p.happy_path[0]["steps"]
    assert not steps[0].startswith("Unverified")
    assert steps[1].startswith("Unverified — not checked against the mobile app's inputs (rule P):")
    assert steps[2] == "Open the results."
    assert changes["unchecked"] == ["happy_path:0"]


@pytest.mark.asyncio
async def test_fully_backed_case_is_left_alone_and_recorded_performable():
    case = {"title": "Utah seller sheet", "surface": "mobile",
            "steps": ["Pick a street address from the autocomplete list.", "Open the results."], "expected": "x"}
    verdict = {"id": "happy_path:0", "verdict": "performable", "client": "the mobile app", "reason": "ok",
               "steps": [{"step": 1, "kind": "action", "input": "AddressAutocomplete", "performable": True,
                          "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3"},
                         {"step": 2, "kind": "observe", "performable": True}]}
    p = plan(case)
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    assert changes["performable"] == ["happy_path:0"]
    assert p.happy_path[0]["steps"] == case["steps"]
    assert p.happy_path[0]["surface_check"]["status"] == "performable"


@pytest.mark.asyncio
async def test_a_case_qa_already_passed_is_only_marked():
    original = county_only_case()
    p = plan(original)
    changes, _ = await si.run_surface_input_critic(
        ScriptedLLM([NOT_PERFORMABLE]), FakeGitHub(), p, DEV,
        protected_titles=frozenset({original["title"]}))
    case = p.happy_path[0]
    assert changes["marked"] == ["happy_path:0"]
    assert case["surface"] == "mobile" and len(case["steps"]) == 4
    assert case["steps"][0].startswith("Not performable on the mobile app (acme/calculator-mobile) — ")


@pytest.mark.asyncio
async def test_a_case_with_no_verdict_is_marked_unchecked_not_passed():
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([]), FakeGitHub(), p, DEV)
    assert changes["unchecked"] == ["happy_path:0"]
    assert p.happy_path[0]["steps"][0].startswith("Unverified — not checked against")


# --- the check never claims to have run when it did not ----------------------

@pytest.mark.asyncio
async def test_critic_failure_is_a_gap_and_changes_nothing():
    p = plan(county_only_case())
    changes, gap = await si.run_surface_input_critic(ScriptedLLM([], error=RuntimeError("down")),
                                                     FakeGitHub(), p, DEV)
    assert "could not run (RuntimeError)" in gap and "1 UI case(s)" in gap
    assert p.happy_path[0]["steps"] == COUNTY_ONLY_STEPS


@pytest.mark.asyncio
async def test_no_linked_prs_is_a_gap():
    p = plan(county_only_case())
    _, gap = await si.run_surface_input_critic(ScriptedLLM([]), FakeGitHub(), p, [{"pull_requests": []}])
    assert "not checked against their surface's inputs" in gap


def test_only_runnable_ui_cases_are_checked():
    p = plan(county_only_case(),
             {"title": "api", "surface": "backend_http", "steps": []},
             county_only_case(title="covered", covered_by_unit_test=True),
             county_only_case(title="gone", reachability={"reachable": False}))
    assert [i["id"] for i in si.collect_surface_items(p)] == ["happy_path:0"]


def test_build_check_steps_are_not_input_actions():
    case = {"surface": "mobile", "steps": ["Build check: confirm the image contains 3feb40e.", "Tap Seller."]}
    assert si._unbacked_action_steps(case, {}, set()) == [2]


@pytest.mark.parametrize("source,ok", [
    (f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3", True),
    (f"{MOBILE_REPO}:{MOBILE_ADDRESS}", True),
    (f"{MOBILE_REPO}:PropertyAddress.tsx:3-9", True),
    (f"{WEB_REPO}:{MOBILE_ADDRESS}:3", False),
    ("src/screens/PropertyAddress.tsx:3", False),
    (f"{MOBILE_REPO}:{MOBILE_ADDRESS} (testID \"address-input\")", True),
    (f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3-9 (AddressAutocomplete)", True),
    ("unverified", False),
    (None, False),
])
def test_citations_count_only_for_files_read(source, ok):
    assert si.cited_read(source, {(MOBILE_REPO, MOBILE_ADDRESS)}) is ok


# --- the generator is told the rule ------------------------------------------

def test_prompt_and_schema_carry_rule_p():
    assert "SURFACE INPUT RULE P" in SYSTEM_PROMPT
    assert "leave the city blank" in SYSTEM_PROMPT  # the SK-2948 shape, as the BAD example
    assert "(P)" in SYSTEM_PROMPT.split("FINAL CHECKLIST")[1]
    assert "surface_inputs" in TEST_CASE_SCHEMA["properties"]


# --- found in the live SK-2948 run -------------------------------------------

ROUTE = {"surface": "mobile", "client": "the mobile app", "can_trigger": True,
         "how": "set the profile service area, then leave the property address empty",
         "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:2"}


@pytest.mark.asyncio
async def test_a_working_route_with_no_usable_rewrite_keeps_the_case_and_names_the_route():
    """Run 1 routed a case out while its own `clients` showed a mobile route
    that works. A case that can be run must not leave the checklist."""
    bad_alt = {**WEB_ALT, "inputs": [{"step": 1, "input": "City", "source": "acme/x:never/read.tsx:1"}]}
    verdict = {**NOT_PERFORMABLE, "alternatives": [bad_alt], "clients": [ROUTE]}
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    assert changes["marked"] == ["happy_path:0"] and not changes["routed_out"]
    case = p.happy_path[0]
    assert "reachability" not in case
    assert case["needs_manual_verification"] is True
    assert case["steps"][0].startswith("Not performable on the mobile app (acme/calculator-mobile)")
    assert "Use instead, on the mobile app: set the profile service area" in case["steps"][0]
    assert case["surface_check"]["status"] == "marked_route_exists"
    assert _no_unperformable_mobile_city_step(p)


@pytest.mark.asyncio
async def test_a_route_cited_from_an_unread_file_does_not_keep_the_case():
    verdict = {**NOT_PERFORMABLE, "alternatives": [],
               "clients": [{**ROUTE, "source": "acme/x:never/read.tsx:1"}]}
    p = plan(county_only_case())
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    assert changes["routed_out"] == ["happy_path:0"]


@pytest.mark.asyncio
async def test_an_observe_step_the_surface_never_shows_is_marked():
    """The fee-source banner renders only in __DEV__ builds, so a tester on a
    release build cannot 'confirm the fee source metadata'."""
    case = {"title": "Utah seller sheet", "surface": "mobile", "expected": "x",
            "steps": ["Pick a street address from the autocomplete list.",
                      "Confirm the fee source metadata shows the formula path."]}
    verdict = {"id": "happy_path:0", "verdict": "performable", "client": "the mobile app", "reason": "ok",
               "steps": [{"step": 1, "kind": "action", "input": "AddressAutocomplete", "performable": True,
                          "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:3"},
                         {"step": 2, "kind": "observe", "performable": False,
                          "why": "the fee source banner renders only when __DEV__ is true",
                          "source": f"{MOBILE_REPO}:{MOBILE_ADDRESS}:1"}]}
    p = plan(case)
    changes, _ = await si.run_surface_input_critic(ScriptedLLM([verdict]), FakeGitHub(), p, DEV)
    steps = p.happy_path[0]["steps"]
    assert steps[1].startswith("Not observable on the mobile app — the fee source banner renders only when __DEV__")
    assert p.happy_path[0]["surface_check"]["not_observable_steps"] == [2]
    assert changes["marked"] == ["happy_path:0"] and not changes["performable"]


# --- plan_service records what the check did ---------------------------------

@pytest.mark.asyncio
async def test_plan_service_records_verdicts_and_gaps(monkeypatch):
    from src.app.services import plan_service

    async def fake_critic(llm, github, test_plan, dev_infos, **kw):
        return {"rewritten": ["happy_path:4"], "reasons": {"happy_path:4": "no city field"}}, \
            "the surface input critic ran out of turns, so 1 UI case(s) were not checked"

    monkeypatch.setattr(plan_service, "run_surface_input_critic", fake_critic)
    provenance: dict = {}
    await plan_service._check_surface_inputs(None, plan(), [], provenance, exploration=None, label="SK-2948")
    assert provenance["surface_inputs"]["rewritten"] == ["happy_path:4"]
    assert provenance["surface_inputs"]["reasons"] == {"happy_path:4": "no city field"}
    assert "not checked" in provenance["critics_unavailable"][0]


@pytest.mark.asyncio
async def test_the_critic_never_replaces_the_expected_result():
    """A live run put an instruction ("Drop the fee-source-metadata step") in
    the alternative's expected field. The pass criterion must not come from it."""
    alt = {**WEB_ALT, "expected": "Drop the fee-source-metadata step: the banner is dev-only."}
    p = plan(county_only_case())
    await si.run_surface_input_critic(ScriptedLLM([{**NOT_PERFORMABLE, "alternatives": [alt]}]),
                                      FakeGitHub(), p, DEV)
    case = p.happy_path[0]
    assert case["expected"] == "No buyer or lender Closing Protection Letter line appears."
    assert case["surface_check"]["expected_note"].startswith("Drop the fee-source-metadata step")
