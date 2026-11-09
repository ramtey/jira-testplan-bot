"""Rules AA–AG: every expected result is something a tester can see on the
deployed environment (src/app/deployed_env_checks.py).

Each test is one failure seen in generated plans for an HTTP service, written
generically (``acme`` placeholders): no ticket is special-cased.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.app import deployed_env_checks as de
from src.app import plan_recheck as pr
from src.app.case_disposition import CODE_REVIEW_KEY, route_out_of_uat
from src.app.source_facts import SourceFile, SourceIndex

REPO = "acme/acme-api"
ROUTER = "src/routes/split.ts"
USER_ROUTER = "src/routes/user.ts"
APP = "src/app.ts"
USER_CTRL = "src/controllers/user.ts"


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": [],
            "security_negative_tests": [], "code_review_checks": [], "risks_and_gaps": []}
    return SimpleNamespace(**{**base, **sections})


def _case(**kw):
    return {"title": "Split a document", "steps": ["POST /split/process with a 3-page PDF"],
            "expected": "201 with the split ids.", "how_to_verify": "The response body.",
            "surface": "backend_http", "environment": "integ — https://api-integ.acme.test",
            "expected_verified": True, "expected_source": f"{ROUTER}:14", **kw}


def _index(files: dict[str, str]):
    idx = SourceIndex()
    for path, text in files.items():
        idx.files[(REPO, path)] = SourceFile(REPO, path, "h" * 40, text)
    idx.refs[REPO] = "h" * 40
    idx.trees[REPO] = list(files)
    return idx


FILES = {
    ROUTER: "import { Router } from 'express'\nconst r = Router()\nr.post('/split/process', handler)\n",
    USER_ROUTER: "r.get('/user/:id/firebase', getFirebase)\nr.put('/user/:id/firebase', putFirebase)\n",
    APP: "app.use('/', splitRouter)\napp.use('/', userRouter)\n",
    USER_CTRL: "export const getFirebase = (req, res) => res.json({ registered: true })\n",
}


# --- AC: malformed URLs are answered at the edge ------------------------------

def test_a_dot_segment_url_with_a_pinned_400_becomes_any_4xx_and_the_handler_status_moves_to_a_unit_test():
    case = _case(title="Reject a traversal id", steps=["GET /user/%2e%2e/firebase with a valid token"],
                 expected="400 Bad Request with 'invalid user id' from the new validation.",
                 expected_source=f"{USER_ROUTER}:1")
    plan = _plan(edge_cases=[case])
    report: dict = {}
    assert de.check_malformed_urls(plan, report) == 1
    assert case["expected"].startswith("Rejected before reaching downstream services, with no downstream call")
    assert "Any 4xx" in case["expected"] and "400" in case["expected"]  # the old prediction is named, not pinned
    assert case["expected_verified"] is False
    assert "no call was made" in case["how_to_verify"]
    entry = plan.code_review_checks[0]
    assert "400" in entry["title"] and "%2e%2e" in entry["title"]
    assert "calls the handler directly" in entry["suggested_test"]
    assert report["findings"][0]["rule"] == "AC"


def test_every_malformed_url_shape_is_detected():
    for path in ("/user/../x", "/user/./x", "/a//b", "/user/%2F", "/user/%252e", "/café", "/a\\b",
                 "/" + "a" * 1100):
        assert de.malformed_url_reason(path), path
    for path in ("/user/123/firebase", "/search", "/user/a%40b.com"):
        assert de.malformed_url_reason(path) is None, path


def test_a_long_path_described_in_words_is_detected():
    case = _case(steps=["Send GET with a path of 10,000 characters to /user"], expected="Returns 414.")
    plan = _plan(edge_cases=[case])
    assert de.check_malformed_urls(plan, {}) == 1
    assert "Any 4xx" in case["expected"]


def test_a_malformed_url_is_left_alone_when_the_plan_proved_it_reaches_the_handler():
    case = _case(steps=["GET /user/%2e%2e/firebase"], expected="Returns 400.",
                 reaches_handler_source="deploy/ingress.yaml:12")
    assert de.check_malformed_urls(_plan(edge_cases=[case]), {}) == 0
    assert case["expected"] == "Returns 400."


def test_a_cli_path_with_dot_dot_is_not_a_url():
    case = _case(surface="cli", steps=["cd /repo/../other && npm test"], expected="Exit status 1.")
    assert de.check_malformed_urls(_plan(edge_cases=[case]), {}) == 0


def test_a_2xx_for_a_malformed_url_is_marked_not_rewritten_to_a_rejection():
    case = _case(steps=["GET /files/a//b"], expected="Returns 200 with the file.")
    assert de.check_malformed_urls(_plan(edge_cases=[case]), {}) == 1
    assert "(rule AC)" in case["expected"] and "Returns 200" in case["expected"]


def test_a_moved_handler_status_reaches_the_code_review_list():
    case = _case(steps=["GET /user/%2e%2e/firebase"], expected="Returns 400.")
    plan = _plan(edge_cases=[case])
    de.check_malformed_urls(plan, {})
    out = route_out_of_uat(plan)
    assert any("400" in c["title"] for c in out[CODE_REVIEW_KEY])
    assert plan.edge_cases == [case]  # the case stays, with the edge outcome


# --- AA: routes come from the router ------------------------------------------

def test_a_step_calling_a_path_with_no_route_source_is_marked():
    case = _case(steps=["POST /split-and-assign/process with a 3-page PDF"])
    plan = _plan(happy_path=[case])
    report: dict = {}
    assert de.check_routes(plan, report, _index(FILES)) == 1
    assert case["steps"][0].startswith("Route not checked against the router (rule AA): POST /split-and-assign/process")


def test_a_route_source_whose_file_lacks_the_path_is_downgraded():
    case = _case(steps=["POST /data-extract/run"],
                 route_sources=[{"method": "POST", "path": "/data-extract/run", "source": f"{ROUTER}:3"}])
    plan = _plan(happy_path=[case])
    assert de.check_routes(plan, {}, _index(FILES)) == 1
    assert "'data-extract' of /data-extract/run is not in" in case["steps"][0]


def test_a_route_found_in_its_cited_file_passes_and_params_match_values():
    case = _case(steps=["GET /user/123/firebase", "POST https://api-integ.acme.test/split/process"],
                 route_sources=[{"method": "GET", "path": "/user/:id/firebase", "source": f"{USER_ROUTER}:1",
                                 "mount_source": f"{APP}:2"},
                                {"method": "POST", "path": "/split/process", "source": f"{ROUTER}:3"}])
    report: dict = {}
    assert de.check_routes(_plan(happy_path=[case]), report, _index(FILES)) == 0
    assert not report.get("findings")


def test_an_unread_route_file_is_unchecked_not_flagged():
    case = _case(route_sources=[{"method": "POST", "path": "/split/process", "source": "src/elsewhere.ts:3"}])
    report: dict = {}
    assert de.check_routes(_plan(happy_path=[case]), report, _index(FILES)) == 0
    assert "could not be read" in report["not_run"][0]


def test_route_matching():
    assert de.route_matches("/user/:id/firebase", "/user/abc/firebase")
    assert de.route_matches("/user/{id}", "/user/<id>")
    assert de.route_matches("/files/*", "/files/a/b")
    assert not de.route_matches("/split/process", "/split-and-assign/process")


# --- AB: response fields come from the response code --------------------------

def test_a_field_the_api_does_not_return_with_no_other_check_is_marked_not_visible():
    case = _case(steps=["GET /user/123/firebase"], expected="The response contains the stored `token`.",
                 response_fields=[{"field": "token", "source": f"{USER_CTRL}:1", "returned": False}])
    assert de.check_response_fields(_plan(happy_path=[case]), {}, _index(FILES)) == 1
    assert case["expected"].startswith("Not visible from outside (rule AB)")


def test_a_field_the_api_does_not_return_with_another_check_adds_it_to_how_to_verify():
    case = _case(response_fields=[{"field": "token", "source": f"{USER_CTRL}:1", "returned": False,
                                   "alternate_check": "the PUT response, or users.firebase_token"}])
    assert de.check_response_fields(_plan(happy_path=[case]), {}, _index(FILES)) == 0
    assert "check it via: the PUT response" in case["how_to_verify"]


def test_a_field_claimed_returned_but_absent_from_the_cited_file_is_downgraded():
    case = _case(response_fields=[{"field": "data.token", "source": f"{USER_CTRL}:1", "returned": True}])
    assert de.check_response_fields(_plan(happy_path=[case]), {}, _index(FILES)) == 1
    assert "'data.token' is not in" in case["expected"]


def test_a_body_field_read_with_no_response_fields_is_marked():
    case = _case(expected="200; the response body contains `token`.")
    assert de.check_response_fields(_plan(happy_path=[case]), {}, _index(FILES)) == 1


def test_the_async_entry_reads_cited_files_not_in_the_index():
    class GH:
        async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=None):
            return {p: FILES[p] for p in paths if p in FILES}

    idx = _index({ROUTER: FILES[ROUTER]})
    idx.trees[REPO] = list(FILES)
    case = _case(steps=["GET /user/1/firebase"],
                 route_sources=[{"method": "GET", "path": "/user/:id/firebase", "source": f"{USER_ROUTER}:1"}])
    report: dict = {}
    asyncio.run(de.check_routes_and_shapes(_plan(happy_path=[case]), report, index=idx, github=GH()))
    assert not report.get("findings") and not report.get("not_run")


# --- AD: counts follow the environment layout ----------------------------------

TOPOLOGY = [{"environment": "integ", "in_front": "Cloudflare → ALB → ingress", "replicas": "3",
             "replicas_source": "deploy/values-integ.yaml:12",
             "state": [{"what": "rate limiter", "scope": "per_pod", "source": "src/middleware/rateLimit.ts:8"}]}]


def test_a_per_pod_limit_on_three_pods_becomes_the_range_a_tester_sees():
    case = _case(expected="The 301st request in a minute gets 429; the limit is 300 requests per minute per user.")
    plan = _plan(edge_cases=[case], environment_topology=TOPOLOGY)
    assert de.check_limits(plan, {}) == 1
    assert "between 300 and 900" in case["expected"]
    assert "3 pods (deploy/values-integ.yaml:12)" in case["expected"]


def test_a_limit_with_no_layout_read_is_record_what_you_see():
    case = _case(expected="After 300 requests per minute the API returns 429.")
    assert de.check_limits(_plan(edge_cases=[case]), {}) == 1
    assert case["expected"].startswith("Count depends on the deployment layout (rule AD)")


def test_a_shared_limiter_keeps_the_exact_count():
    topo = [{**TOPOLOGY[0], "state": [{"what": "rate limiter", "scope": "shared", "source": "redis.ts:3"}]}]
    case = _case(expected="After 300 requests per minute the API returns 429.")
    assert de.check_limits(_plan(edge_cases=[case], environment_topology=topo), {}) == 0


# --- AE: how each case is checked -----------------------------------------------

def test_a_unit_test_only_case_leaves_the_graded_sections():
    case = _case(verification_channel="unit_test_only", unit_test_ref="src/user.test.ts › rejects '..'")
    plan = _plan(edge_cases=[case])
    assert de.route_unit_only_cases(plan, {}) == 1
    out = route_out_of_uat(plan)
    assert plan.edge_cases == [] and out[CODE_REVIEW_KEY][0]["suggested_test"] == "src/user.test.ts › rejects '..'"


def test_channels_are_inferred_from_the_surface_and_labelled():
    a, b = _case(), _case(surface="manual_only", how_to_verify="")
    plan = _plan(happy_path=[a, b])
    assert de.label_channels(plan, {}) == 1
    assert a["verification_channel"] == "outside_http"
    assert a["how_to_verify"].startswith("Checked from outside (HTTP response):")
    assert "(rule AE)" in b["how_to_verify"]


# --- AF: setup each case needs --------------------------------------------------

def test_unconfirmed_setup_marks_the_case_blocked():
    case = _case(required_setup=[{"need": "a second tenant", "status": "unconfirmed", "how_to_get": "ask Platform"}])
    assert de.check_setup(_plan(happy_path=[case]), {}) == 1
    assert case["blocked_by_setup"].startswith("a second tenant (unconfirmed: ask Platform)")
    assert case["preconditions"].startswith("BLOCKED BY SETUP (rule AF)")


def test_special_data_the_plan_did_not_list_blocks_the_case():
    case = _case(steps=["Sign in as a deactivated user", "GET /user/1/firebase"])
    assert de.check_setup(_plan(happy_path=[case]), {}) == 1
    assert "deactivated user" in case["blocked_by_setup"]


def test_confirmed_setup_does_not_block():
    case = _case(required_setup=[{"need": "a second tenant", "status": "exists"}])
    assert de.check_setup(_plan(happy_path=[case]), {}) == 0


def test_the_blocked_mark_is_never_hoisted_into_a_shared_precondition():
    cases = [_case(title=f"c{i}", steps=["Sign in as a deactivated user"]) for i in range(3)]
    plan = _plan(happy_path=cases)
    pr.recheck_before_save(plan, {})
    assert all(c["preconditions"].startswith("BLOCKED BY SETUP") for c in cases)


# --- AG: complete cases, and repo claims from the repo's files -------------------

def test_a_case_with_no_title_is_titled_and_one_with_no_steps_moves_out():
    untitled = _case(title="  ")
    stepless = _case(title="Firebase token", steps=[])
    empty = {"title": "", "steps": [], "expected": ""}
    plan = _plan(happy_path=[untitled, stepless, empty])
    report: dict = {}
    de.check_complete_cases(plan, report)
    assert untitled["title"] == "POST /split/process with a 3-page PDF"
    assert empty not in plan.happy_path
    out = route_out_of_uat(plan)
    assert plan.happy_path == [untitled]
    assert out[CODE_REVIEW_KEY][0]["title"] == "Firebase token"


def test_a_no_test_scripts_claim_is_contradicted_by_package_json():
    case = _case(expected="acme-api has no test scripts, so run the requests by hand.")
    evidence = [{"repo": REPO, "commands": {"package_scripts": ["build", "test", "test:e2e"]}, "test_sources": {}}]
    assert de.check_repo_claims(_plan(happy_path=[case]), {}, evidence) == 1
    assert case["expected"].startswith("Contradicted by the repo's files (rule AG)")
    assert "test, test:e2e" in case["expected"]


def test_a_repo_claim_with_no_build_files_read_is_unverified():
    gap = {"gap": "The repo has no tests.", "impact": "x"}
    assert de.check_repo_claims(_plan(risks_and_gaps=[gap]), {}, []) == 1
    assert gap["gap"].startswith("Unverified (rule AG)")


def test_a_true_no_scripts_claim_is_kept():
    case = _case(expected="The repo has no scripts.")
    evidence = [{"repo": REPO, "commands": {"package_scripts": []}, "test_sources": {}}]
    assert de.check_repo_claims(_plan(happy_path=[case]), {}, evidence) == 0


# --- wiring -----------------------------------------------------------------------

def test_recheck_before_routing_runs_the_new_rules():
    class LLM:
        pass

    case = _case(steps=["GET /user/%2e%2e/firebase"], expected="Returns 400.")
    plan = _plan(edge_cases=[case])
    report: dict = {}
    asyncio.run(de.recheck_before_routing(plan, report, index=_index(FILES)))
    rules = {f["rule"] for f in report["findings"]}
    assert {"AC", "AA"} <= rules


# --- false positives seen on stored plans ------------------------------------------

def test_a_json_escaped_svg_namespace_is_not_a_backslash_path():
    case = _case(steps=['POST /upload with body "<svg xmlns=\\"http://www.w3.org/2000/svg\\">"'],
                 expected="Returns 400.")
    assert de.check_malformed_urls(_plan(edge_cases=[case]), {}) == 0


def test_a_vendor_limit_or_alert_threshold_is_not_this_services_limit():
    for text in ("The vendor's 2 req/s limit returns 429 upstream; the client retries.",
                 "The alerting rule fires after 5 calls in 1 hour over the threshold."):
        case = _case(expected=text)
        assert de.check_limits(_plan(edge_cases=[case]), {}) == 0, text


def test_coverage_claims_and_other_nouns_are_not_repo_script_claims():
    evidence = [{"repo": REPO, "commands": {"package_scripts": ["test"]}, "test_sources": {}}]
    for text in ("The repo has no test file for the changed code at merge commit abc.",
                 "There is no automated test covering the new transition behaviour.",
                 "RLAS has no CAR TEST schema, so the AC cannot be met.",
                 "package.json has no script 'x' (rule Y)."):
        gap = {"gap": text}
        assert de.check_repo_claims(_plan(risks_and_gaps=[gap]), {}, evidence) == 0, text
    gap = {"gap": "The repo has no test runner at merge commit abc."}
    assert de.check_repo_claims(_plan(risks_and_gaps=[gap]), {}, evidence) == 1


def test_a_client_retrying_a_vendor_429_is_not_this_services_limit():
    case = _case(expected="CAR answers HTTP 429 above 2 req/s. The failure is transient — retried and fallen back "
                          "to SQS, never classified as rejected.")
    assert de.check_limits(_plan(edge_cases=[case]), {}) == 0


def test_a_file_based_route_is_found_in_the_files_path():
    page = "app/reset-password/page.tsx"
    case = _case(steps=["GET /reset-password", "GET /reset-password?token=x"],
                 route_sources=[{"method": "GET", "path": "/reset-password", "source": f"{page}:1"}])
    report: dict = {}
    assert de.check_routes(_plan(happy_path=[case]), report, _index({**FILES, page: "export default Page"})) == 0


def test_several_unbacked_steps_in_one_case_are_one_finding():
    case = _case(steps=["GET /a", "GET /b", "GET /c"])
    report: dict = {}
    assert de.check_routes(_plan(happy_path=[case]), report, _index(FILES)) == 1
    assert len(report["findings"]) == 1 and all("(rule AA)" in s for s in case["steps"])


def test_a_prefix_the_gateway_strips_still_matches_the_registered_route():
    topo = [{"environment": "integ", "replicas": "3",
             "in_front": "Cloudflare → NLB → Envoy gateway (/api/ → /) → pod"}]
    case = _case(steps=["GET /api/user/123/firebase"],
                 route_sources=[{"method": "GET", "path": "/user/:id/firebase", "source": f"{USER_ROUTER}:1"}])
    assert de.check_routes(_plan(happy_path=[case], environment_topology=topo), {}, _index(FILES)) == 0
    # Without the gateway saying so, the extra prefix is not assumed.
    case2 = _case(steps=["GET /api/user/123/firebase"],
                  route_sources=[{"method": "GET", "path": "/user/:id/firebase", "source": f"{USER_ROUTER}:1"}])
    assert de.check_routes(_plan(happy_path=[case2]), {}, _index(FILES)) == 1
