"""Rule AI: an endpoint is named by its full mounted path (src/app/endpoint_paths.py,
src/app/mount_tracer.py).

The regression is built from a real plan, sanitized: a tRPC procedure defined
in an admin sub-router, mounted two levels deep (``reportBundle`` in the admin
router, ``admin`` in the root router) under a server prefix (``/trpc``). The
plan named it ``reportBundle.generateBundle``, the leaf of the router file, so
every call the tester made got "No procedure found on path", and the no-token
case could not be graded. No ticket is special-cased: the same tests cover
Express prefixes, Fastify ``register`` prefixes and Next.js file routes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.app import deployed_env_checks as de
from src.app import mount_tracer as mt
from src.app.endpoint_paths import check_endpoint_paths
from src.app.llm_client import SYSTEM_PROMPT as PROMPT, TEST_CASE_SCHEMA
from src.app.source_facts import SourceFile, SourceIndex

REPO = "acme/acme-calc"
DEF = "apps/server/src/api/router/admin/report-bundle.ts"
ADMIN = "apps/server/src/api/router/admin/index.ts"
ROOT = "apps/server/src/api/root.ts"
SERVER = "apps/server/src/index.ts"
PAGE = "apps/admin/src/app/admin/report-bundle/page.tsx"
FULL = "/trpc/admin.reportBundle.generateBundle"

TRPC_FILES = {
    "apps/server/package.json": "{}",
    "apps/admin/package.json": "{}",
    DEF: """import { z } from 'zod'
import { createTRPCRouter, adminProcedure } from '../../trpc'
import { requireAdminRole } from '../../middleware/roles'

export const reportBundleRouter = createTRPCRouter({
  status: adminProcedure.query(() => ({ ok: true })),
  generateBundle: adminProcedure
    .use(requireAdminRole)
    .input(z.object({ accountId: z.string() }))
    .mutation(async ({ input }) => buildBundle(input.accountId)),
})
""",
    ADMIN: """import { createTRPCRouter } from '../../trpc'
import { usersRouter } from './users'
import { reportBundleRouter } from './report-bundle'

export const adminRouter = createTRPCRouter({
  users: usersRouter,
  reportBundle: reportBundleRouter,
})
""",
    "apps/server/src/api/router/admin/users.ts": "export const usersRouter = createTRPCRouter({ list: adminProcedure.query(() => []) })\n",
    ROOT: """import { createTRPCRouter } from './trpc'
import { adminRouter } from './router/admin'
import { calcRouter } from './router/calc'

export const appRouter = createTRPCRouter({
  calc: calcRouter,
  admin: adminRouter,
})
export type AppRouter = typeof appRouter
""",
    "apps/server/src/api/trpc.ts": "export const createTRPCRouter = t.router\nexport const adminProcedure = t.procedure\n",
    SERVER: """import Fastify from 'fastify'
import { fastifyTRPCPlugin } from '@trpc/server/adapters/fastify'
import { appRouter } from './api/root'

const server = Fastify()
server.register(fastifyTRPCPlugin, {
  prefix: '/trpc',
  trpcOptions: { router: appRouter, createContext },
})
server.listen({ port: 3000 })
""",
    PAGE: """'use client'
import { api } from '~/trpc/react'

export default function ReportBundlePage() {
  const generate = api.admin.reportBundle.generateBundle.useMutation()
  return <button onClick={() => generate.mutate({ accountId })}>Generate</button>
}
""",
}


class GH:
    """Serves files at the PR ref, counting reads."""

    def __init__(self, files):
        self.files, self.reads = files, []

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=None):
        self.reads += paths
        return {p: self.files[p] for p in paths if p in self.files}


def _index(files: dict[str, str], read: list[str] | None = None, tree: list[str] | None = None):
    idx = SourceIndex()
    for path in (read if read is not None else list(files)):
        idx.files[(REPO, path)] = SourceFile(REPO, path, "h" * 40, files[path])
    idx.refs[REPO] = "h" * 40
    idx.trees[REPO] = tree if tree is not None else list(files)
    return idx


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": [],
            "security_negative_tests": [], "code_review_checks": [], "risks_and_gaps": [],
            "environment_topology": []}
    return SimpleNamespace(**{**base, **sections})


def _security_case(title, auth, expected, **kw):
    return {
        "title": title,
        "steps": [f"Send the reportBundle.generateBundle mutation with body {{\"accountId\": \"<test account>\"}} and {auth}",
                  "Read the response status and error code"],
        "expected": expected,
        "how_to_verify": "The response status and body.",
        "expected_verified": True,
        "persona": auth,
        "request": {"method": "mutation", "route": "reportBundle.generateBundle", "auth": auth, "protocol": "trpc"},
        # As the generator wrote it: the defining router file and the next mount only.
        "route_sources": [{"method": "POST", "path": "reportBundle.generateBundle", "source": f"{DEF}:7",
                           "mount_source": f"{ADMIN}:7"}],
        **kw,
    }


def _sk3060_cases():
    return [
        _security_case("No token is rejected", "no Authorization header",
                       "401 with tRPC error code UNAUTHORIZED; no bundle is generated."),
        _security_case("Garbage token is rejected", "Authorization: Bearer not-a-jwt",
                       "401 with tRPC error code UNAUTHORIZED."),
        _security_case("Non-admin token is rejected", "Bearer token for a non-admin test user",
                       "403 with tRPC error code FORBIDDEN from requireAdminRole."),
        _security_case("Admin token succeeds (control)", "Bearer token for the admin test user",
                       "200 with a bundle download URL."),
    ]


def _run(plan, idx, gh=None):
    report: dict = {}
    asyncio.run(de.check_routes_and_shapes(plan, report, index=idx, github=gh))
    return report


# --- the regression: two nested routers under a prefixed mount ----------------------

def test_the_tracer_follows_two_router_levels_and_the_server_prefix():
    t = asyncio.run(mt.trace(_index(TRPC_FILES), None, f"{DEF}:7", procedure="generateBundle"))
    assert t.full_path == FULL and t.missing is None
    assert [(l.kind, l.key, l.source) for l in t.links] == [
        ("definition", "generateBundle", f"{DEF}:7"),
        ("router_key", "reportBundle", f"{ADMIN}:7"),
        ("router_key", "admin", f"{ROOT}:7"),
        ("url_prefix", "/trpc", f"{SERVER}:6"),
    ]
    assert ("requireAdminRole", f"{DEF}:7") in t.guards


def test_security_cases_naming_the_leaf_get_the_full_mounted_path_and_cite_every_mount():
    cases = _sk3060_cases()
    plan = _plan(security_negative_tests=cases)
    # Only the defining file was read up front; the climb reads the rest at the PR ref.
    gh = GH(TRPC_FILES)
    report = _run(plan, _index(TRPC_FILES, read=[DEF]), gh)
    assert ROOT in gh.reads and SERVER in gh.reads
    for c in cases:
        assert c["request"]["route"] == FULL
        assert c["steps"][0].startswith(f"Send the {FULL} mutation")
        assert "reportBundle.generateBundle mutation" not in c["steps"][0].replace(FULL, "")
        how = c["how_to_verify"]
        for cited in (f"{DEF}:7", f"{ADMIN}:7", f"{ROOT}:7", f"{SERVER}:6"):
            assert cited in how, cited
        assert "(rule AI)" not in c["expected"] and c["expected_verified"] is True
        assert "requireAdminRole" in c["preconditions"]
        assert c["route_sources"][0]["path"] == FULL
    fixed = [f for f in report["findings"] if f["rule"] == "AI" and f["action"] == "fixed"]
    assert any(FULL in f["reason"] and "'reportBundle.generateBundle'" in f["reason"] for f in fixed)
    assert not [f for f in report["findings"] if f["action"] in ("downgraded", "marked")]


def test_a_negative_case_says_how_to_tell_rejection_from_route_not_found():
    no_token = _sk3060_cases()[0]
    _run(_plan(security_negative_tests=[no_token]), _index(TRPC_FILES))
    how = no_token["how_to_verify"]
    assert "If the response is 404 with tRPC error with data.code \"NOT_FOUND\"" in how
    assert "No procedure found on path" in how and "not the check passing" in how
    assert "Rejected by the check under test looks like: 401, error code UNAUTHORIZED." in how


def test_a_negative_case_with_no_rejection_status_is_downgraded():
    case = _security_case("Garbage token is rejected", "Authorization: Bearer not-a-jwt", "The request is rejected.")
    report = _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert case["expected"].startswith("Cannot tell rejection from route-not-found (rule AI)")
    assert case["expected_verified"] is False
    assert any(f["rule"] == "AI" and f["action"] == "downgraded" for f in report["findings"])


def test_a_rejection_that_is_also_a_404_needs_a_body_that_tells_them_apart():
    case = _security_case("Other tenant's bundle is hidden", "Bearer token for a second tenant",
                          "404 so the bundle's existence is not leaked.")
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert "look the same" in case["expected"] and case["expected_verified"] is False


def test_the_cited_caller_is_checked_and_named_when_it_agrees():
    case = _sk3060_cases()[2]
    case["route_sources"][0]["caller_source"] = f"{PAGE}:5"
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert f"Same path the client calls: {PAGE}:5." in case["how_to_verify"]


def test_a_caller_that_disagrees_marks_the_case_and_picks_no_path():
    files = {**TRPC_FILES, PAGE: TRPC_FILES[PAGE].replace("api.admin.reportBundle", "api.admin.reports")}
    case = _sk3060_cases()[0]
    case["route_sources"][0]["caller_source"] = f"{PAGE}:5"
    report = _run(_plan(security_negative_tests=[case]), _index(files))
    assert case["steps"][0].startswith("Endpoint path disputed (rule AI): the client at")
    assert "calls 'admin.reports.generateBundle'" in case["steps"][0] and FULL in case["steps"][0]
    assert case["request"]["route"] == "reportBundle.generateBundle"  # not rewritten
    assert case["expected_verified"] is False
    assert any(f["rule"] == "AI" and f["action"] == "downgraded" for f in report["findings"])


def test_a_plan_chain_that_disagrees_with_the_code_is_disputed():
    case = _sk3060_cases()[0]
    case["route_sources"][0]["path"] = "/api/trpc/admin.reportBundle.generateBundle"
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert case["steps"][0].startswith("Endpoint path disputed (rule AI): the plan traces /api/trpc/")


def test_an_untraceable_chain_is_marked_unverified_not_guessed():
    # The root router and the server entry are not in the repo the bot can read.
    files = {k: v for k, v in TRPC_FILES.items() if k not in (ROOT, SERVER)}
    case = _sk3060_cases()[0]
    case["route_sources"][0].pop("mount_source")
    report = _run(_plan(security_negative_tests=[case]), _index(files))
    step = case["steps"][0]
    assert step.startswith("Endpoint path unverified (rule AI): reportBundle.generateBundle: no file mounts 'adminRouter'")
    assert "confirm the path from a real client call" in step
    assert case["request"]["route"] == "reportBundle.generateBundle"
    assert case["expected_verified"] is False
    down = [f for f in report["findings"] if f["rule"] == "AI" and f["action"] == "downgraded"]
    assert len(down) == 1 and "no file mounts 'adminRouter'" in down[0]["reason"]


def test_with_no_code_to_read_the_plans_own_full_chain_stands():
    case = _sk3060_cases()[1]
    case["request"]["route"] = FULL
    case["steps"][0] = case["steps"][0].replace("reportBundle.generateBundle", FULL)
    case["route_sources"] = [{"method": "POST", "path": FULL, "source": f"{DEF}:7", "mount_chain": [
        {"key": "reportBundle", "source": f"{ADMIN}:7"}, {"key": "admin", "source": f"{ROOT}:7"},
        {"key": "/trpc", "source": f"{SERVER}:6"}]}]
    report = _run(_plan(security_negative_tests=[case]), None)
    assert "(rule AI)" not in " ".join(case["steps"]) and case["expected_verified"] is True
    assert f"Endpoint path traced (rule AI): {FULL} — defined at {DEF}:7, mounted as 'reportBundle'" in case["how_to_verify"]
    assert not [f for f in report.get("findings", []) if f["action"] == "downgraded"]


def test_with_no_code_to_read_a_leaf_only_citation_is_unverified():
    case = _sk3060_cases()[0]
    case["route_sources"][0].pop("mount_source")
    _run(_plan(security_negative_tests=[case]), None)
    assert case["steps"][0].startswith("Endpoint path unverified (rule AI): reportBundle.generateBundle: only the "
                                       "router that defines it is cited")


def test_the_generators_own_unverified_note_is_kept_as_the_marker():
    case = _sk3060_cases()[0]
    case["route_sources"][0].pop("mount_source")
    case["route_sources"][0]["source"] = ""
    case["route_sources"][0]["unverified"] = "the root router file was not readable"
    _run(_plan(security_negative_tests=[case]), None)
    assert "the root router file was not readable" in case["steps"][0]


def test_a_tail_of_a_wrong_path_is_not_rewritten():
    case = _sk3060_cases()[0]
    case["steps"][0] = "Send the generateBundle mutation via /trpc/reportBundle.generateBundle"
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert case["steps"][0].startswith("Endpoint path disputed (rule AI): the case calls /trpc/reportBundle.generateBundle")


# --- Express: nested routers under a prefixed mount ------------------------------------

EXPRESS = {
    "package.json": "{}",
    "src/routes/orders.ts": """import { Router } from 'express'
export const ordersRouter = Router()
ordersRouter.post('/:id/refund', refundOrder)
""",
    "src/routes/v2.ts": """import { Router } from 'express'
import { ordersRouter } from './orders'
export const v2Router = Router()
v2Router.use('/orders', ordersRouter)
""",
    "src/app.ts": """import express from 'express'
import { v2Router } from './routes/v2'
import { requireAuth } from './middleware/auth'
const app = express()
app.use('/api/v2', requireAuth, v2Router)
app.listen(8080)
""",
}


def test_an_express_route_under_two_prefixes_is_named_in_full_and_keeps_its_values():
    case = {"title": "Refund an order", "surface": "backend_http", "persona": "signed-in buyer",
            "steps": ["POST https://api.acme.test/orders/123/refund with a valid token"],
            "expected": "200 and the order is refunded.", "how_to_verify": "Response body.",
            "expected_verified": True,
            "route_sources": [{"method": "POST", "path": "/orders/:id/refund", "source": "src/routes/orders.ts:3"}]}
    report = _run(_plan(happy_path=[case]), _index(EXPRESS, tree=list(EXPRESS)))
    assert case["steps"][0] == "POST https://api.acme.test/api/v2/orders/123/refund with a valid token"
    assert "/api/v2/orders/:id/refund" in case["how_to_verify"]
    assert "src/routes/v2.ts:4" in case["how_to_verify"] and "src/app.ts:5" in case["how_to_verify"]
    assert "requireAuth" in case["preconditions"]
    assert not [f for f in report["findings"] if f["action"] in ("downgraded", "marked")]


def test_a_fastify_register_prefix_is_part_of_the_path():
    files = {
        "package.json": "{}",
        "src/routes/invoices.ts": "export default async function invoiceRoutes(fastify) {\n"
                                  "  fastify.get('/:id', getInvoice)\n}\n",
        "src/server.ts": "import Fastify from 'fastify'\nimport invoiceRoutes from './routes/invoices'\n"
                         "const app = Fastify()\napp.register(invoiceRoutes, { prefix: '/invoices' })\n"
                         "app.listen({ port: 3000 })\n",
    }
    t = asyncio.run(mt.trace(_index(files), None, "src/routes/invoices.ts:2", sub_path="/:id", method="GET"))
    assert t.full_path == "/invoices/:id" and t.framework == "fastify"


def test_next_file_routes():
    assert mt.next_file_route("apps/web/src/app/api/orders/[id]/refund/route.ts") == "/api/orders/:id/refund"
    assert mt.next_file_route("apps/web/app/(admin)/api/users/route.ts") == "/api/users"
    assert mt.next_file_route("pages/api/health.ts") == "/api/health"
    assert mt.next_file_route("src/lib/route-helpers.ts") is None


# --- no false endpoints -----------------------------------------------------------------

def test_dotted_names_that_are_not_procedures_are_left_alone():
    case = _sk3060_cases()[3]
    case["expected"] = "200; response.data.url is set and user.id matches; report-bundle.ts is unchanged."
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert "response.data.url is set and user.id matches; report-bundle.ts" in case["expected"]


def test_a_case_with_no_endpoint_is_untouched():
    case = {"title": "Totals update", "steps": ["Open the calculator", "Enter 2 and 3"], "expected": "5 shows.",
            "how_to_verify": "Screen.", "surface": "web_ui"}
    before = dict(case)
    report: dict = {}
    assert asyncio.run(check_endpoint_paths(_plan(happy_path=[case]), report, index=_index(TRPC_FILES))) == 0
    assert case == before and not report.get("findings")


# --- the generator is told ------------------------------------------------------------

def test_the_prompt_and_schema_carry_rule_ai():
    assert "**AI. Name every endpoint by its full mounted path." in PROMPT
    assert "endpoint path unverified:" in PROMPT
    props = TEST_CASE_SCHEMA["properties"]
    route = props["route_sources"]["items"]["properties"]
    assert {"mount_chain", "caller_source", "guards", "unverified"} <= set(route)
    assert "rejection_vs_not_found" in props


# --- shapes seen on stored plans ----------------------------------------------------------

def test_a_procedure_with_no_route_source_is_found_among_the_read_files_and_traced():
    case = _sk3060_cases()[0]
    case.pop("route_sources")
    report = _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert case["request"]["route"] == FULL
    assert case["route_sources"][0]["source"] == f"{DEF}:7"
    assert any(f["rule"] == "AI" and f["action"] == "fixed" for f in report["findings"])


def test_a_firebase_rewrite_is_its_own_mount():
    case = {"title": "Update a user", "surface": "backend_http", "steps": ["POST /update-user with the API key"],
            "expected": "200.", "how_to_verify": "Response.", "expected_verified": True,
            "route_sources": [{"method": "POST", "path": "/update-user", "source": "firebase.json:29-46"}]}
    report = _run(_plan(happy_path=[case]), None)
    assert "(rule AI)" not in case["steps"][0] and case["expected_verified"] is True
    assert not [f for f in report.get("findings", []) if f["rule"] == "AI" and f["action"] == "downgraded"]


FASTIFY_NESTED = {
    "package.json": "{}",
    "src/routes/admin/admin.ts": "import { FastifyInstance } from 'fastify'\n"
                                 "export async function adminRoutes(server: FastifyInstance) {\n"
                                 "  server.get<{ Querystring: { q: string } }>('/users', async () => [])\n"
                                 "  server.post('/users/:userId/activate_admin', activate)\n}\n",
    "src/routes/stream-text.ts": "export async function streamTextRoutes(server: FastifyInstance) {\n"
                                 "  server.withTypeProvider<ZodTypeProvider>().route({\n"
                                 "    method: 'POST',\n    url: '/chat/:uuid',\n    schema: { body: z.object({}) },\n"
                                 "    handler: chat,\n  })\n}\n",
    "src/app.ts": "import Fastify from 'fastify'\nimport { adminRoutes } from './routes/admin/admin'\n"
                  "import { streamTextRoutes } from './routes/stream-text'\n"
                  "export const buildServer = () => {\n  const fastify = Fastify()\n"
                  "  fastify.register(streamTextRoutes, { prefix: '/api/stream-text' })\n"
                  "  fastify.register(\n    async (adminInstance) => {\n"
                  "      adminInstance.addHook('onRequest', adminMiddleware)\n"
                  "      adminInstance.register(adminRoutes)\n    },\n    { prefix: '/api/admin' }\n  )\n"
                  "  return fastify\n}\n",
}


def test_fastify_typed_routes_and_an_inline_prefixed_plugin_are_followed():
    idx = _index(FASTIFY_NESTED)
    t = asyncio.run(mt.trace(idx, None, "src/routes/admin/admin.ts:3", sub_path="/users", method="GET"))
    assert t.full_path == "/api/admin/users", t
    t = asyncio.run(mt.trace(idx, None, "src/routes/stream-text.ts:2", sub_path="/chat/:uuid", method="POST"))
    assert t.full_path == "/api/stream-text/chat/:uuid", t


def test_an_unrelated_server_in_the_same_file_is_not_taken_for_the_entry():
    files = {**FASTIFY_NESTED, "src/app.ts": FASTIFY_NESTED["src/app.ts"].replace(
        "      adminInstance.register(adminRoutes)\n    },\n    { prefix: '/api/admin' }\n  )\n",
        "      adminInstance.register(adminRoutes)\n    },\n    adminOptions\n  )\n")}
    t = asyncio.run(mt.trace(_index(files), None, "src/routes/admin/admin.ts:3", sub_path="/users", method="GET"))
    # The prefix is in a variable it cannot read: the trace says so instead of answering "/users".
    assert t.full_path != "/users"


def test_class_routers_mounted_through_a_list_are_not_guessed():
    files = {"package.json": "{}",
             "src/routes/envelopes.route.ts": "export default class EnvelopesRoute {\n  public path = '/envelopes'\n"
                                              "  constructor() {\n    this.router.get(`${this.path}/digisign`, h)\n  }\n}\n"}
    t = asyncio.run(mt.trace(_index(files), None, "src/routes/envelopes.route.ts:4", sub_path="/envelopes/digisign",
                             method="GET"))
    assert t.full_path is None and "class" in t.missing


def test_a_control_case_is_not_held_to_the_negative_rule_and_an_unverified_negative_still_gets_the_404_note():
    control = _sk3060_cases()[3]
    control["expected"] = "The bundle is generated and the token in it is redacted."
    no_token = _sk3060_cases()[0]
    for c in (control, no_token):
        c["route_sources"][0].pop("mount_source")
    files = {k: v for k, v in TRPC_FILES.items() if k not in (ROOT, SERVER)}
    _run(_plan(security_negative_tests=[control, no_token]), _index(files))
    assert "Cannot tell rejection" not in control["expected"] and "route was not found" not in control["how_to_verify"]
    assert no_token["steps"][0].startswith("Endpoint path unverified (rule AI)")
    assert "No procedure found on path" in no_token["how_to_verify"]


def test_a_latency_figure_is_not_read_as_the_rejection_status():
    case = _security_case("Bad token is rejected fast", "Authorization: Bearer not-a-jwt",
                          "Rejected within 300 ms with 401 UNAUTHORIZED.")
    _run(_plan(security_negative_tests=[case]), _index(TRPC_FILES))
    assert "Rejected by the check under test looks like: 401" in case["how_to_verify"]


def test_a_word_is_not_absorbed_by_a_parameter():
    from src.app.endpoint_paths import _suffix_match

    assert not _suffix_match("/get-user/:userId", "/get-user/")
    assert _suffix_match("/api/v2/orders/:id/refund", "/orders/123/refund")
    assert _suffix_match("/api/v2/orders/:id", "/orders/{id}")


def test_a_catch_all_route_is_not_taken_for_a_named_path():
    files = {"package.json": "{}",
             "src/app.ts": "import express from 'express'\nconst app = express()\n"
                           "app.post('*', handler)\napp.listen(80)\n"}
    t = asyncio.run(mt.trace(_index(files), None, "src/app.ts:3", sub_path="/function/expire-packages/retry",
                             method="POST"))
    assert t is None or t.full_path is None


def test_an_unread_router_file_named_after_the_key_is_found_and_a_role_builder_is_a_guard():
    # As in the original plan: the PR never touched the router file, the plan cited
    # no route, and the procedure is built from a role guard, not a *Procedure.
    files = {**TRPC_FILES, DEF: TRPC_FILES[DEF].replace(
        "  generateBundle: adminProcedure\n    .use(requireAdminRole)\n", "  generateBundle: requireAdminRole\n")}
    case = _sk3060_cases()[0]
    case.pop("route_sources")
    gh = GH(files)
    report = _run(_plan(security_negative_tests=[case]), _index(files, read=[], tree=list(files)), gh)
    assert DEF in gh.reads
    assert case["request"]["route"] == FULL
    assert case["route_sources"][0]["source"] == f"{DEF}:7"
    assert "requireAdminRole" in case["preconditions"]
    assert any(f["rule"] == "AI" and f["action"] == "fixed" for f in report["findings"])


def test_the_climb_passes_through_a_re_export_barrel():
    files = {**TRPC_FILES,
             "apps/server/src/api/index.ts": "export { appRouter } from './root'\nexport * from './trpc'\n",
             SERVER: TRPC_FILES[SERVER].replace("from './api/root'", "from './api'")}
    t = asyncio.run(mt.trace(_index(files), None, f"{DEF}:7", procedure="generateBundle"))
    assert t.full_path == FULL, t


def test_a_barrel_that_imports_then_exports_is_passed_through():
    files = {**TRPC_FILES,
             "apps/server/src/api/index.ts": "import { appRouter } from './root'\nimport { createCallerFactory } from './trpc'\n"
                                             "const createCaller = createCallerFactory(appRouter)\n"
                                             "export { appRouter, createCaller }\n",
             SERVER: TRPC_FILES[SERVER].replace("from './api/root'", "from './api'")}
    t = asyncio.run(mt.trace(_index(files), None, f"{DEF}:7", procedure="generateBundle"))
    assert t.full_path == FULL, t


def test_a_router_written_as_a_trpc_router_record_is_followed():
    files = {**TRPC_FILES, DEF: (
        "import type { TRPCRouterRecord } from '@trpc/server'\n"
        "const labels = { generateBundle: 'Generate' }\n"
        "export const reportBundleRouter = {\n"
        "  status: adminProcedure.query(() => ({ ok: true })),\n"
        "  generateBundle: adminProcedure\n    .use(requireAdminRole)\n    .mutation(async () => build()),\n"
        "} satisfies TRPCRouterRecord\n")}
    t = asyncio.run(mt.trace(_index(files), None, f"{DEF}:5", procedure="generateBundle"))
    assert t.full_path == FULL, t
    assert t.links[0].source == f"{DEF}:5"  # the definition, not the unrelated `labels` key on line 2


def test_a_root_level_procedure_named_without_the_url_prefix_is_rewritten_not_disputed():
    from src.app.endpoint_paths import _relation

    assert _relation("titleFeed.getListingInsight", "/trpc/titleFeed.getListingInsight", "trpc", set()) == "partial"
    assert _relation("/api/trpc/titleFeed.getListingInsight", "/trpc/titleFeed.getListingInsight", "trpc", set()) == "other"
    assert _relation("feed.getListingInsight", "/trpc/titleFeed.getListingInsight", "trpc", set()) == "other"
