"""A route prefix is read from the router registration, reached from the server entry.

Two plans had security cases downgraded under rule AA as "prefix unknown"
(SK-2835 plan 748, SK-3059 plan 747) although the code says exactly where
the routes are served. The layout, sanitized here:

* ``server.ts`` creates the Fastify app and calls ``registerRoutes(app)`` —
  a plain function call, not a ``register``/``use`` mount;
* ``routes/index.ts`` defines ``registerRoutes`` and registers each route
  file with a literal ``prefix``.

Both checks (the rule-AI tracer and the AA route resolver) stopped at
``routes/index.ts``: nothing *mounted* it, and it is not the server entry.
The call from the entry is the mount. Only the PR's own route files are in
the index at the start, as on a real run: the registration file and the
entry have to be read, not assumed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.app import deployed_env_checks as de
from src.app import mount_tracer as mt
from src.app.code_presence import resolve_route
from src.app.source_facts import SourceFile, SourceIndex

REPO = "acme/acme-media"
APP = "apps/media-api"
SERVER = f"{APP}/src/server.ts"
ROUTES = f"{APP}/src/routes/index.ts"
PODCAST = f"{APP}/src/routes/podcast.ts"
CRON = f"{APP}/src/routes/cron.ts"

FILES = {
    f"{APP}/package.json": "{}",
    SERVER: """import Fastify from 'fastify'
import { registerRoutes } from './routes'

async function start() {
  const app = Fastify({ logger: true })
  await registerRoutes(app)
  await app.listen({ port: Number(process.env.PORT) || 3000, host: '0.0.0.0' })
}

start()
""",
    ROUTES: """import { FastifyInstance } from 'fastify'
import { podcastRoutes } from './podcast'
import { cronRoutes } from './cron'

export async function registerRoutes(app: FastifyInstance) {
  await app.register(podcastRoutes, { prefix: '/api/podcasts' })
  await app.register(cronRoutes, { prefix: '/api/cron' })
}
""",
    PODCAST: """import { FastifyInstance } from 'fastify'
import { requireAuth } from '../auth'

export async function podcastRoutes(app: FastifyInstance) {
  app.post('/', { preHandler: requireAuth }, createPodcast)
  app.get('/:id', { preHandler: requireAuth }, getPodcast)
}
""",
    CRON: """import { FastifyInstance } from 'fastify'
import { verifyCronSecret } from '../auth'

export async function cronRoutes(app: FastifyInstance) {
  app.post('/trigger/:slug', { preHandler: verifyCronSecret }, triggerJob)
}
""",
    f"{APP}/src/auth.ts": "export const requireAuth = async () => {}\nexport const verifyCronSecret = async () => {}\n",
}


class GH:
    """Serves the repo at the PR ref, recording what was read."""

    def __init__(self, files):
        self.files, self.reads = files, []

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=None):
        self.reads += list(paths)
        return {p: self.files[p] for p in paths if p in self.files}


def _index(read):
    idx = SourceIndex()
    for path in read:
        idx.files[(REPO, path)] = SourceFile(REPO, path, "h" * 40, FILES[path])
    idx.refs[REPO] = "h" * 40
    idx.trees[REPO] = list(FILES)
    return idx


def _plan(cases):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": [],
            "security_negative_tests": cases, "code_review_checks": [], "risks_and_gaps": [],
            "environment_topology": []}
    return SimpleNamespace(**base)


def _case(title, step, expected, route_sources=None):
    case = {
        "title": title,
        "steps": [step, "Read the response status"],
        "expected": expected,
        "how_to_verify": "The response status.",
        "expected_verified": True,
    }
    if route_sources is not None:
        case["route_sources"] = route_sources
    return case


# --- the tracer (rule AI) ------------------------------------------------------------

def test_the_tracer_follows_the_entry_call_through_the_registration_file():
    gh = GH(FILES)
    idx = _index([PODCAST, CRON])
    t = asyncio.run(mt.trace(idx, gh, f"{PODCAST}:5", sub_path="/", method="POST"))
    assert t.full_path == "/api/podcasts" and t.missing is None, t
    assert [(l.kind, l.key, l.source) for l in t.links] == [
        ("definition", "/", f"{PODCAST}:5"),
        ("path_prefix", "/api/podcasts", f"{ROUTES}:6"),
        ("path_prefix", "", f"{SERVER}:6"),
    ]
    t = asyncio.run(mt.trace(idx, gh, f"{CRON}:5", sub_path="/trigger/:slug", method="POST"))
    assert t.full_path == "/api/cron/trigger/:slug" and t.missing is None, t


def test_a_registration_function_never_called_from_the_entry_stays_unresolved():
    # Defined, never called: there is no chain, so the tracer must not invent one.
    files = {**FILES, SERVER: FILES[SERVER].replace("  await registerRoutes(app)\n", "")}
    idx = SourceIndex()
    for p in (PODCAST, CRON):
        idx.files[(REPO, p)] = SourceFile(REPO, p, "h" * 40, files[p])
    idx.refs[REPO], idx.trees[REPO] = "h" * 40, list(files)
    t = asyncio.run(mt.trace(idx, GH(files), f"{PODCAST}:5", sub_path="/", method="POST"))
    assert t.full_path is None and "registerRoutes" in t.missing


# --- the route resolver (rule AA) ----------------------------------------------------------

def test_the_route_resolver_composes_the_chain_from_the_entry_call():
    idx = _index(list(FILES))
    found = resolve_route(idx, "POST", "/api/cron/trigger/nightly")
    assert found.match is not None, found.where()
    assert found.match.pattern == "/api/cron/trigger/:slug"
    assert found.match.mount_source() == f"{ROUTES}:7"
    found = resolve_route(idx, "POST", "/api/podcasts")
    assert found.match is not None and found.match.pattern == "/api/podcasts"


def test_an_unrelated_call_with_the_app_is_not_a_mount():
    # registerRoutes in a file the entry never calls: still no prefix chain.
    files = {**FILES, SERVER: FILES[SERVER].replace("await registerRoutes(app)", "await setupLogging(app)")}
    idx = SourceIndex()
    for p, text in files.items():
        idx.files[(REPO, p)] = SourceFile(REPO, p, "h" * 40, text)
    found = resolve_route(idx, "POST", "/api/cron/trigger/nightly")
    assert found.match is None and found.unmounted


# --- end to end: the re-check reads the files and applies no AA downgrade ---------------------

def test_no_rule_aa_downgrade_when_the_entry_registers_the_routes():
    cases = [
        # As SK-2835's generator wrote it: the route file, and the path as the tester calls it.
        _case("Creating a podcast with no token is rejected", "POST /api/podcasts with no Authorization header",
              "401; no podcast is created.",
              route_sources=[{"method": "POST", "path": "/api/podcasts", "source": f"{PODCAST}:5"}]),
        # As SK-3059's: the sub-path only, cited from the cron file.
        _case("A cron trigger with a wrong secret is rejected",
              "POST /api/cron/trigger/nightly with header x-cron-secret: wrong",
              "401; the job does not run.",
              route_sources=[{"method": "POST", "path": "/trigger/:slug", "source": f"{CRON}:5"}]),
        # No citation at all: resolved from the router code alone.
        _case("Reading a podcast with no token is rejected", "GET /api/podcasts/123 with no Authorization header",
              "401."),
    ]
    gh = GH(FILES)
    idx = _index([PODCAST, CRON])
    report: dict = {}
    asyncio.run(de.check_routes_and_shapes(_plan(cases), report, index=idx, github=gh))

    # The registration file and the entry were read, not assumed.
    assert {SERVER, ROUTES} <= set(gh.reads)
    findings = report.get("findings", [])
    assert not [f for f in findings if f["rule"] == "AA"], findings
    assert not [f for f in findings if f["action"] in ("downgraded", "marked")], findings
    for c in cases:
        assert c["expected_verified"] is True, c
        assert not any("unverified" in s.lower() or "unchecked" in s.lower() for s in c["steps"]), c["steps"]
    assert cases[0]["steps"][0] == "POST /api/podcasts with no Authorization header"
    assert cases[1]["steps"][0].startswith("POST /api/cron/trigger/nightly")


def test_a_registration_file_named_for_its_job_outside_the_route_directory_is_read_and_followed():
    reg = f"{APP}/src/http/register-routes.ts"
    main = f"{APP}/src/main.ts"
    files = {k: v for k, v in FILES.items() if k not in (SERVER, ROUTES)}
    files[reg] = FILES[ROUTES].replace("'./podcast'", "'../routes/podcast'").replace("'./cron'", "'../routes/cron'")
    files[main] = FILES[SERVER].replace("'./routes'", "'./http/register-routes'")
    case = _case("A cron trigger with a wrong secret is rejected",
                 "POST /api/cron/trigger/nightly with header x-cron-secret: wrong", "401; the job does not run.",
                 route_sources=[{"method": "POST", "path": "/trigger/:slug", "source": f"{CRON}:5"}])
    gh = GH(files)
    idx = SourceIndex()
    for p in (PODCAST, CRON):
        idx.files[(REPO, p)] = SourceFile(REPO, p, "h" * 40, files[p])
    idx.refs[REPO], idx.trees[REPO] = "h" * 40, list(files)
    report: dict = {}
    asyncio.run(de.check_routes_and_shapes(_plan([case]), report, index=idx, github=gh))
    assert {reg, main} <= set(gh.reads)
    assert not [f for f in report.get("findings", []) if f["action"] in ("downgraded", "marked")], report
    assert case["expected_verified"] is True
