"""Rule AM: a request-level case states who sends it and expects the layer that answers first.

SK-3174 (plan 752 v1, sanitized in tests/fixtures/sk3174/): regression lines
sent POST /mcp with no auth to a route whose onRequest hook checks the bearer
token before the handler's batch check, expected the app's 421 for a wrong
Host through a Cloudflare-proxied hostname, and asked for a JWKS outage only
the platform team could cause. See src/app/request_layers.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

from src.app import plan_recheck as pr
from src.app import request_layers as rl
from src.app.source_facts import SourceFile, SourceIndex

FIXTURE = Path(__file__).parent / "fixtures" / "sk3174"
V1 = json.loads((FIXTURE / "plan_752_v1.json").read_text())
REPO = "acme/mcp-server"
SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests", "regression_checklist")
OUTAGE, BATCH, HOST = 2, 3, 4


def _index(skip: tuple[str, ...] = ()) -> SourceIndex:
    idx = SourceIndex()
    for p in (FIXTURE / "repo").rglob("*"):
        rel = str(p.relative_to(FIXTURE / "repo"))
        if p.is_file() and rel not in skip:
            idx.files[(REPO, rel)] = SourceFile(REPO, rel, "h" * 40, p.read_text())
    return idx


def _plan(topology: str = "environment_topology_v1", **overrides):
    base = {s: copy.deepcopy(V1[s]) for s in SECTIONS}
    base.update(overrides)
    return SimpleNamespace(**base, environment_topology=copy.deepcopy(V1.get(topology) or []),
                           shared_preconditions=[])


def _run(plan, index=None) -> dict:
    report: dict = {}
    rl.check_request_layers(plan, report, index)
    return report


# --- 1. auth stated -------------------------------------------------------------------------

def test_batch_line_gets_the_principal_and_the_hook_that_answers_first():
    plan = _plan()
    _run(plan, _index())
    line = plan.regression_checklist[BATCH]
    assert "Runs as:" in line and "Auth: Bearer token for test user A (files.read)" in line
    assert "the auth hook at server/src/app.ts:26 runs first" in line and "answers 401" in line
    # Auth goes before the steps; the Expected (the handler's batch answer) is now reachable and kept.
    assert line.index("Runs as:") < line.index("Steps:")
    assert "Expected: 400 JSON-RPC -32600 'Batch requests are not supported.'" in line


def test_without_the_code_the_plans_own_no_token_case_is_the_evidence():
    plan = _plan()
    _run(plan, None)
    line = plan.regression_checklist[BATCH]
    assert "Runs as:" in line
    assert "the plan's own no-credentials case “POST /mcp tools/call with no Authorization header” expects 401" in line


def test_a_line_that_already_names_its_principal_is_left_alone():
    line = ("🟡 Batch — at risk via server/src/app.ts. Steps: 1. As user A, POST /mcp with a JSON array of two "
            "tools/call. Expected: 400 -32600. Check: response.")
    plan = _plan(regression_checklist=[line])
    _run(plan, _index())
    assert plan.regression_checklist == [line]


def test_a_line_about_authentication_itself_gets_no_token_added():
    line = ("🔴 No token — at risk via server/src/app.ts. Steps: 1. POST /mcp with no Authorization header. "
            "Expected: 401 invalid_token. Check: response.")
    plan = _plan(regression_checklist=[line])
    _run(plan, _index())
    assert plan.regression_checklist == [line]


def test_a_public_route_exempted_by_the_hook_is_not_flagged():
    line = ("🟢 Metadata — at risk via server/src/app.ts. Steps: 1. GET /.well-known/oauth-protected-resource/mcp. "
            "Expected: 200 JSON with scopes_supported. Check: body.")
    plan = _plan(regression_checklist=[line], security_negative_tests=[])
    _run(plan, _index())
    assert plan.regression_checklist == [line]


def test_graded_case_gets_credentials_and_a_runs_as_precondition():
    case = {"title": "Batch JSON-RPC is refused", "surface": "backend_http",
            "steps": ["POST https://mcp.example.test/mcp with a JSON array of two tools/list calls."],
            "expected": "HTTP 400 with JSON-RPC error -32600.", "how_to_verify": "response"}
    plan = _plan(edge_cases=[case])
    report = _run(plan, _index())
    c = plan.edge_cases[0]
    assert c["preconditions"].startswith("Runs as:") and c["credentials"].startswith("Bearer token for test user A")
    assert any(f["rule"] == "AM" and "named no principal" in f["reason"] for f in report["findings"])


# --- 2. auth answers first ----------------------------------------------------------------------

def test_a_no_token_case_expecting_a_later_checks_status_is_marked():
    case = {"title": "Batch without a token", "security_category": "unauth_sweep", "persona": "anonymous",
            "request": {"method": "POST", "route": "/mcp", "auth": "none — no Authorization header"},
            "steps": ["Send a JSON array of two calls."], "expected": "HTTP 400, JSON-RPC -32600.",
            "credentials": "none"}
    plan = _plan(security_negative_tests=V1["security_negative_tests"] + [case])
    _run(plan, _index())
    exp = plan.security_negative_tests[1]["expected"]
    assert exp.startswith("Answered first by the auth layer (rule AM): with no credentials the request gets 401")
    assert plan.security_negative_tests[1]["expected_verified"] is False


# --- 3. edge answers first --------------------------------------------------------------------------

def test_wrong_host_behind_an_unruled_out_edge_points_at_the_unit_test():
    plan = _plan()
    _run(plan, _index())
    line = plan.regression_checklist[HOST]
    assert "Runs as:" in line  # the host check runs after the auth hook too
    exp = line[line.index("Expected:"):]
    assert exp.startswith("Expected: From outside, a CDN or WAF in front of the public hostname")
    assert "record the status and the server, via and cf-ray headers" in exp
    assert "asserted by server/test/app.test.ts › answers a wrong Host with the same 421 as before" in exp


def test_wrong_host_names_cloudflare_when_the_topology_shows_it():
    plan = _plan("environment_topology_cdn")
    _run(plan, _index())
    assert "Expected: From outside, Cloudflare may answer first" in plan.regression_checklist[HOST]


def test_wrong_host_without_a_test_is_not_externally_verifiable():
    plan = _plan()
    _run(plan, _index(skip=("server/test/app.test.ts",)))
    assert "not externally verifiable" in plan.regression_checklist[HOST]


def test_a_topology_that_rules_out_a_cdn_leaves_the_host_check_alone():
    topo = [{"environment": "integ", "in_front": "no CDN: DNS only → envoy ingress → pod", "replicas": "2"}]
    line = ("🟡 Wrong host — at risk via server/src/app.ts. Runs as: user A. Auth: Bearer token for user A. "
            "Steps: 1. POST /mcp with Host: evil.example. Expected: 421 misdirected_request. Check: response.")
    plan = _plan(regression_checklist=[line])
    plan.environment_topology = topo
    _run(plan, _index())
    assert plan.regression_checklist == [line]


# --- 4. infrastructure-only trigger -------------------------------------------------------------------

def test_outage_line_cites_the_unit_test():
    plan = _plan()
    _run(plan, _index())
    line = plan.regression_checklist[OUTAGE]
    assert "Covered by server/test/app.test.ts › answers 503 with Retry-After when the key set cannot be fetched" in line


def test_outage_line_without_a_test_is_manual_with_the_owning_team():
    plan = _plan()
    _run(plan, None)
    assert "Manual — needs the platform team (rule AM)" in plan.regression_checklist[OUTAGE]


# --- 5. order unknown ---------------------------------------------------------------------------------------

def test_no_evidence_either_way_holds_an_error_status_to_the_outcome():
    plan = _plan(security_negative_tests=[], happy_path=[])
    _run(plan, None)
    line = plan.regression_checklist[BATCH]
    assert "Order of checks unverified (rule AM)" in line and "Runs as:" not in line


# --- whole pass ------------------------------------------------------------------------------------------

def test_untouched_lines_stay_and_a_second_pass_changes_nothing():
    plan = _plan()
    _run(plan, _index())
    assert plan.regression_checklist[0] == V1["regression_checklist"][0]
    assert plan.regression_checklist[1] == V1["regression_checklist"][1]
    before = copy.deepcopy(plan.regression_checklist)
    assert rl.check_request_layers(plan, {}, _index()) == 0
    assert plan.regression_checklist == before
    assert len(plan.regression_checklist) == len(V1["regression_checklist"])  # progress key: counts unchanged


def test_recheck_before_save_runs_rule_am():
    plan = _plan()
    report: dict = {}
    pr.recheck_before_save(plan, report, index=_index())
    assert sum(1 for f in report["findings"] if f["rule"] == "AM") == 4
    assert "Runs as:" in plan.regression_checklist[BATCH]


def test_unrelated_middleware_followed_by_token_code_is_not_an_auth_hook():
    src = ("const app = express();\napp.use(express.json());\nfunction readToken(h) { return h; }\n"
           "app.post('/orders', handler);\n")
    idx = SourceIndex()
    idx.files[(REPO, "src/server.js")] = SourceFile(REPO, "src/server.js", "h", src)
    assert rl._auth_hooks(idx) == []


def test_prompt_has_rule_am_and_its_self_check():
    from src.app.llm_client import SYSTEM_PROMPT

    assert "**AM. A request states who sends it" in SYSTEM_PROMPT
    assert "say who it runs as and with what auth" in SYSTEM_PROMPT and "(AM)" in SYSTEM_PROMPT


def test_a_line_that_only_mentions_a_request_is_not_a_request():
    line = "🟢 Run the regression API suite and confirm no spec still references PUT /mcp. Expected: none. Check: grep."
    plan = _plan(regression_checklist=[line])
    _run(plan, _index())
    assert plan.regression_checklist == [line]


def test_tester_side_network_cuts_are_not_infrastructure_triggers():
    case = {"title": "A reply that fails partway keeps the chat", "steps": [
        "While it streams, cut the network: turn off Wi-Fi.", "Stop at the Okta consent screen and record it."],
            "expected": "An inline Retry appears.", "credentials": "user A session"}
    plan = _plan(edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"] == "An inline Retry appears."


def test_a_forwarded_colon_in_prose_is_not_a_forwarded_header():
    case = {"title": "Caller disconnects while the upload is being forwarded: downstream is cancelled",
            "steps": ["POST /mcp as user A and kill the client."], "expected": "No 500 is logged.",
            "credentials": "user A token"}
    plan = _plan(edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"] == "No 500 is logged."


def test_a_wrong_host_sent_to_the_origin_over_a_port_forward_is_left_alone():
    # Rule AM point 2: changing the input so it passes the edge is the other correct answer.
    case = {"title": "Wrong Host sent straight to the app gets 421", "credentials": "Bearer token for user A",
            "steps": ["Over a kubectl port-forward, POST https://127.0.0.1:3000/mcp with Host: evil.example."],
            "expected": "421 {\"error\":\"misdirected_request\"}"}
    plan = _plan("environment_topology_cdn", edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"] == "421 {\"error\":\"misdirected_request\"}"


def test_a_wrong_host_case_is_held_to_the_edge_even_when_its_url_does_not_parse():
    case = {"title": "Wrong Host is refused", "credentials": "Bearer token for user A",
            "steps": ["Send the request to <public url> with Host: evil.example."],
            "expected": "421 {\"error\":\"misdirected_request\"}"}
    plan = _plan("environment_topology_cdn", edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"].startswith("From outside, Cloudflare may answer first (rule AM)")


def test_host_as_a_label_or_an_object_key_is_not_a_host_header():
    case = {"title": "Integ host no longer resolves", "credentials": "none — unauthenticated; VPN only",
            "preconditions": "Host: integ-debugger.example.test (VPN).",
            "steps": ["Run nslookup integ-debugger.example.test.", "Call describe({upstream:{method:'GET', host:'files.test'}})."],
            "expected": "NXDOMAIN."}
    plan = _plan("environment_topology_cdn", edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"] == "NXDOMAIN."


def test_a_case_another_team_must_set_up_names_the_test_and_the_team():
    case = {"title": "Key set unavailable: callers get 503 with Retry-After",
            "steps": ["Engineering deploys a fresh pod with MCP_ISSUER pointed at an address that never answers.",
                      "Runs as: user A. Auth: Bearer <token>. POST /mcp to that pod."],
            "expected": "503 with Retry-After 5 and no WWW-Authenticate.", "credentials": "user A token"}
    plan = _plan(edge_cases=[case])
    _run(plan, _index())
    exp = plan.edge_cases[0]["expected"]
    assert "Asserted by server/test/app.test.ts › answers 503 with Retry-After" in exp
    assert "manual and needs Engineering (rule AM" in exp


def test_an_engineering_failure_offered_as_an_alternative_to_going_offline_is_not_infra_only():
    case = {"title": "Save fails — no success toast", "credentials": "user A session",
            "steps": ["Take the browser offline (or have Engineering force the save request to fail) and click Save."],
            "expected": "An error is shown and no toast."}
    plan = _plan(edge_cases=[case])
    _run(plan, _index())
    assert plan.edge_cases[0]["expected"] == "An error is shown and no toast."


def test_an_outage_the_setup_list_says_another_team_must_stage_is_infra_only():
    case = {"title": "Key set refusing connections: record whether callers get 503 or 401",
            "steps": ["Runs as: user A. Auth: Bearer <token>. Through the port-forward to each pod, POST /mcp."],
            "expected": "503 with Retry-After 5 and no WWW-Authenticate.", "credentials": "user A token",
            "blocked_by_setup": "Needs reconfigured, non-serving pods from the Platform team.",
            "required_setup": [{"need": "Two scratch pods with MCP_ISSUER overridden", "status": "missing",
                                "how_to_get": "Platform team"}]}
    plan = _plan(edge_cases=[case])
    _run(plan, _index())
    exp = plan.edge_cases[0]["expected"]
    assert "Asserted by server/test/app.test.ts › answers 503 with Retry-After" in exp and "the Platform team" in exp
