"""A fact present in the code is never labelled unverified or "not checked".

The regression is SK-3014 plan 734 v1 (sanitized in tests/fixtures/sk3014):

* happy_path:2 step 3 was marked "label per ticket, not found in code" for
  ``softRefusals`` because its one cited file (streamedTextsRepository.ts)
  spells the column ``soft_refusals``. BaseStreamText.ts — another changed
  file — writes ``debug_data.softRefusals`` on every turn.
* happy_path:0 and eight security cases were marked "Route not checked against
  the router (rule AA)" for ``POST /api/stream-text/chat/<uuid>``. app.ts
  registers the routes plugin with ``prefix: '/api/stream-text'`` and
  routes/stream-text.ts declares ``method: 'POST', url: '/chat/:uuid'``. Live
  integ answered the route with 401 for no/invalid tokens. Two defects: the
  path was read as ``.../chat/`` (the ``<uuid>`` placeholder ended it), and a
  case with no ``route_sources`` was never resolved from the router.

The rule is general: search every file read and the diff in every spelling,
compose a route from its registration prefix and its own path across files,
cite file:line when found, say where it looked when not, and re-test every
such mark before the plan is saved.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.app import code_presence as cp
from src.app import deployed_env_checks as de
from src.app import grounding_verification as gv
from src.app.source_facts import SourceFile, SourceIndex

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sk3014" / "plan734_v1.json").read_text())
REPO, REF = FIXTURE["repo"], FIXTURE["ref"]
APP = "apps/agent-coach-api/src/app.ts"
ROUTES = "apps/agent-coach-api/src/routes/stream-text.ts"
BASE = "packages/generate/src/v2/streamText/BaseStreamText.ts"
REPO_TS = "packages/supabase/src/public/repositories/streamedTextsRepository.ts"
MIGRATION = "packages/supabase/migrations/20261117000000_add_streamed_texts_refusal_lock.sql"


def _index(paths=(APP, ROUTES, BASE, REPO_TS)) -> SourceIndex:
    index = SourceIndex()
    index.refs[REPO] = REF
    index.trees[REPO] = list(FIXTURE["files"]) + [MIGRATION]
    for p in paths:
        index.files[(REPO, p)] = SourceFile(REPO, p, REF, FIXTURE["files"][p])
    return index


def _line(path: str, needle: str) -> int:
    return next(i for i, l in enumerate(FIXTURE["files"][path].splitlines(), 1) if needle in l)


def _plan(stored: bool = True) -> SimpleNamespace:
    """The rows as stored (marks on), or as the model wrote them (marks and the
    downgrades the passes recorded stripped)."""
    rows = copy.deepcopy(FIXTURE["plan"])
    if not stored:
        for cases in rows.values():
            for c in cases:
                c["steps"] = [s.split(" — ", 1)[1] if s.startswith("Route not checked") else
                              s.split("): ", 1)[1] if s.startswith(gv.LABEL_MARKER) else s for s in c["steps"]]
                c.pop("grounding_downgrades", None)
                c.pop("unverified_labels", None)
    return SimpleNamespace(happy_path=rows["happy_path"], edge_cases=[], integration_tests=[],
                           security_negative_tests=rows["security_negative_tests"], regression_checklist=[],
                           environment_topology=[])


def _aa_marked(plan) -> list[str]:
    return [f"{sec}:{i}" for sec in ("happy_path", "security_negative_tests")
            for i, c in enumerate(getattr(plan, sec)) if any("(rule AA)" in s for s in c["steps"])]


# --- the identifier ------------------------------------------------------------


def test_a_camelcase_key_written_in_another_changed_file_is_found_at_its_exact_spelling():
    exact, _ = cp.find_identifier(_index(), "softRefusals")
    assert exact is not None and exact.path == BASE
    # The code line, not the comment above it.
    assert exact.line == _line(BASE, "softRefusals: this.softRefusals")
    exact, _ = cp.find_identifier(_index(), "debug_data.softRefusals")
    assert exact.path == BASE


def test_a_different_casing_is_a_variant_never_a_hit():
    """``soft_refusals`` is in the code; ``softRefusalz`` is not. A query on a
    JSON key needs the exact spelling, so a casing variant does not verify."""
    exact, variant = cp.find_identifier(_index((REPO_TS,)), "softRefusals")
    assert exact is None
    assert variant is not None and variant.path == REPO_TS and "soft_refusals" in variant.text


def test_the_diff_is_searched_even_for_files_not_read_whole():
    """A SQL migration is in the diff but is not a source file read whole."""
    index = _index((REPO_TS,))
    index.patches[(REPO, MIGRATION)] = ("@@ -0,0 +1,3 @@\n+alter table streamed_texts\n"
                                        "+  add column blocked_at timestamptz,\n+  add column soft_refusal_count int;\n")
    exact, _ = cp.find_identifier(index, "soft_refusal_count")
    assert exact is not None and (exact.path, exact.line) == (MIGRATION, 3)


def test_happy_path_2_label_is_verified_from_the_other_file_not_downgraded():
    plan = _plan(stored=False)
    report = gv.verify_plan(plan, _index())
    case = plan.happy_path[2]
    assert not any(s.startswith(gv.LABEL_MARKER) for s in case["steps"]), case["steps"]
    assert "unverified_labels" not in case
    assert not [o for o in report["downgraded_claims"] if "softRefusals" in o["claim"]]
    assert report["by_kind"]["label"]["verified"] >= 1


def test_a_label_truly_absent_stays_unverified_and_says_where_it_looked():
    plan = _plan(stored=False)
    plan.happy_path[2]["steps"][2] = "Query soft_refusals, blocked_at, and debug_data->'softRefusalz'."
    gv.verify_plan(plan, _index())
    step = plan.happy_path[2]["steps"][2]
    assert step.startswith(gv.LABEL_MARKER)
    assert "searched 4 file(s) the PRs change or the plan cites, at agent-coach@78690e84" in step


# --- the route -----------------------------------------------------------------


def test_the_route_is_composed_from_the_register_prefix_and_the_plugins_own_path():
    found = cp.resolve_route(_index(), "POST", "/api/stream-text/chat/<uuid>")
    m = found.match
    assert m is not None and m.pattern == "/api/stream-text/chat/:uuid"
    assert m.source() == f"{ROUTES}:{_line(ROUTES, 'method:')}"
    assert m.mount_source() == f"{APP}:{_line(APP, 'register(streamTextRoutes')}"
    assert cp.resolve_route(_index(), "GET", "/api/stream-text/chat/x").match is None
    assert cp.resolve_route(_index(), "POST", "/stream-text/chat/x").match is None


def test_without_the_registration_the_prefix_is_unknown_and_the_mark_says_so():
    found = cp.resolve_route(_index((ROUTES,)), "POST", "/api/stream-text/chat/x")
    assert found.match is None and found.unmounted and found.unmounted[0].path == "/chat/:uuid"
    # A placeholder alone is not a match: /chat-with-tools does not end /x/<id>.
    assert not cp._tail_matches("/chat-with-tools", "/api/stream-text/chat/{newUuid}")
    assert cp._tail_matches("/chat/:uuid", "/api/stream-text/chat/{newUuid}")


@pytest.mark.parametrize("step", [
    "POST /api/stream-text/chat/<uuid> with a message.",
    "POST /api/stream-text/chat/<A's chat uuid> with no Authorization header and the S4 body.",
    "As B, POST /api/stream-text/chat/{uuid} with an off-topic message.",
])
def test_a_placeholder_is_one_segment_not_the_end_of_the_path(step):
    [(method, path)] = de._calls(step)
    assert method == "POST" and len(de._segments(path)) == 4


def test_no_rule_aa_mark_for_any_sk3014_request_and_the_route_is_cited():
    plan = _plan(stored=False)
    report: dict = {}
    de.check_routes(plan, report, _index())
    assert _aa_marked(plan) == []
    sec0 = plan.security_negative_tests[0]
    [r] = sec0["route_sources"]
    assert r["resolved_from_code"] and r["source"].startswith(ROUTES) and r["mount_source"].startswith(APP)
    assert report["routes_resolved_from_code"] >= 8


def test_an_unregistered_route_keeps_its_mark_and_says_where_it_looked():
    plan = _plan(stored=False)
    plan.security_negative_tests[0]["steps"][0] = "POST /api/stream-text/nope/<uuid> with no Authorization header."
    de.check_routes(plan, {}, _index())
    step = plan.security_negative_tests[0]["steps"][0]
    assert step.startswith("Route not checked against the router (rule AA): POST /api/stream-text/nope/<uuid> "
                           "(searched 2 router file(s): app.ts, stream-text.ts) —")


@pytest.mark.asyncio
async def test_the_router_files_are_read_when_no_case_cites_them():
    """Only the changed routes file is in the index; app.ts comes from the tree."""
    index = _index((ROUTES, BASE, REPO_TS))

    class GH:
        async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=None):
            return {p: FIXTURE["files"][p] for p in paths if p in FIXTURE["files"]}

    plan = _plan(stored=False)
    for c in plan.happy_path + plan.security_negative_tests:
        c.pop("route_sources", None)  # nothing cites app.ts
    report: dict = {}
    await de.check_routes_and_shapes(plan, report, index=index, github=GH())
    assert APP in report["router_files_read"]
    assert _aa_marked(plan) == []


# --- the re-test before save ---------------------------------------------------


def test_every_stored_mark_whose_fact_is_in_the_code_is_dropped_and_cited():
    plan = _plan(stored=True)
    assert len(_aa_marked(plan)) == 9  # happy_path:0, security 0-6 and 8, as posted
    cleared = gv.retest_marks(plan, _index())
    assert _aa_marked(plan) == []
    step3 = plan.happy_path[2]["steps"][2]
    assert not step3.startswith(gv.LABEL_MARKER)
    assert step3.endswith(f"(in code: {BASE}:{_line(BASE, 'softRefusals: this.softRefusals')})")
    assert "unverified_labels" not in plan.happy_path[2]
    assert not [d for d in plan.happy_path[2].get("grounding_downgrades") or [] if d.get("kind") == "label"]
    sec0 = plan.security_negative_tests[0]
    assert sec0["steps"][0].startswith("POST /api/stream-text/chat/<A's chat uuid>")
    assert "(in code: app.ts:" in sec0["steps"][0] and "stream-text.ts:" in sec0["steps"][0]
    assert not [d for d in sec0.get("grounding_downgrades") or [] if d["claim"].startswith("rule AA")]
    assert {o.kind for o in cleared} == {"label", "route"} and len(cleared) == 10


def test_a_mark_whose_fact_is_not_in_the_code_stays():
    plan = _plan(stored=True)
    cleared = gv.retest_marks(plan, _index((REPO_TS,)))  # neither BaseStreamText nor the router was read
    assert cleared == []
    assert len(_aa_marked(plan)) == 9
    assert plan.happy_path[2]["steps"][2].startswith(gv.LABEL_MARKER)


@pytest.mark.asyncio
async def test_the_save_time_pass_reports_what_it_cleared():
    plan = _plan(stored=True)
    report = await gv.run_grounding_verification(plan, _index(), None, shown=None, token_sources=None)
    assert report["cleared"] == 10
    assert report["downgraded"] == len(report["downgraded_claims"])


def test_express_class_routers_with_template_paths_mounted_in_a_loop():
    """The express-typescript layout (SK-3083 plan 727): ``${this.path}`` paths
    on a class whose ``path = '/envelopes'``, mounted by
    ``routes.forEach(route => this.app.use('/', route.router))``."""
    app = ("import express from 'express';\nclass App {\n  constructor(routes) {\n    this.app = express();\n"
           "    routes.forEach(route => {\n      this.app.use('/', route.router);\n    });\n  }\n}\n")
    routes = ("import { Router } from 'express';\nclass EnvelopesRoute {\n  public path = '/envelopes';\n"
              "  public router = Router();\n  private initializeRoutes() {\n"
              "    this.router.get(`${this.path}/digisign`, auth, c.list);\n"
              "    this.router.get(`${this.path}/digisign/:id/history`, auth, c.history);\n  }\n}\n")
    index = SourceIndex()
    for p, t in (("src/app.ts", app), ("src/routes/envelopes.route.ts", routes)):
        index.files[("example-org/forms-api", p)] = SourceFile("example-org/forms-api", p, REF, t)
    m = cp.resolve_route(index, "GET", "/envelopes/digisign/{id}/history").match
    assert m is not None and m.pattern == "/envelopes/digisign/:id/history" and m.decl.line == 7
    assert m.mount_source() == "src/app.ts:6"
    assert cp.resolve_route(index, "GET", "/envelopes/digisign").match is not None
    assert cp.resolve_route(index, "GET", "/digisign").match is None


def test_go_servemux_routes_on_a_mux_mounted_under_a_stripped_prefix():
    """Go 1.22 ServeMux (SK-3077 plan 702): routes declared on a parameter
    ``api`` in one file, mounted with StripPrefix in another."""
    register = ("package httpapi\n\nfunc registerAccessRoutes(api *http.ServeMux, a *API) {\n"
                "\tapi.HandleFunc(\"GET /delegations/{id}\", a.Delegation.Get)\n"
                "\tapi.HandleFunc(\"POST /delegations\", a.Delegation.Create)\n}\n")
    routes = ("package main\n\nfunc routes(d deps) http.Handler {\n\tmux := http.NewServeMux()\n"
              "\tapi := http.NewServeMux()\n\thttpapi.Register(api, d.api)\n"
              "\tmux.Handle(\"/api/\", d.auth.Handler(http.StripPrefix(\"/api\", middleware.Metrics(api))))\n"
              "\treturn mux\n}\n")
    index = SourceIndex()
    for p, t in (("v2/internal/httpapi/register.go", register), ("v2/cmd/skytc/routes.go", routes)):
        index.files[("example-org/aqbx", p)] = SourceFile("example-org/aqbx", p, REF, t)
    m = cp.resolve_route(index, "GET", "/api/delegations/{L}").match
    assert m is not None and m.source() == "v2/internal/httpapi/register.go:4"
    assert m.mount_source() == "v2/cmd/skytc/routes.go:7"
    assert cp.resolve_route(index, "PATCH", "/api/delegations/1").match is None
    assert cp.resolve_route(index, "GET", "/delegations/1").match is None


def test_no_router_code_read_says_so_plainly():
    found = cp.resolve_route(_index((BASE,)), "GET", "/schemas")
    assert found.where() == "no route declaration or registration in the 1 file(s) read"
