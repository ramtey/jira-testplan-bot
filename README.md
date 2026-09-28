# jira-testplan-bot

**Turn a Jira ticket into a QA test plan that's grounded in the code that actually changed.**

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![React](https://img.shields.io/badge/react-18.3-blue.svg)

Point it at a ticket key. It reads the ticket, its parent, its linked issues,
the QA discussion in the comments, the merged PR diffs, the Figma file and the
repo's own test conventions — then writes a test plan, checks its own work
against the diff, and posts it back to Jira.

It also analyzes bug tickets ([Bug Lens](docs/FEATURES.md#jira-bug-lens)) for
root cause, fix explanation and regression coverage, and runs the QA hand-off
(pull to testing → pass to UAT → fail back) without leaving the app.

> [!IMPORTANT]
> **Never commit your `.env`** — it holds your Jira, Claude, GitHub and Figma
> tokens. Copy `.env.example` to `.env` and keep `.env` in `.gitignore`. Every
> user brings their own tokens.

---

## How it works

```
ticket key
   ↓
gather context   Jira ticket · parent epic · linked issues · QA comments
                 GitHub PR diffs · Confluence specs · Figma · repo testIDs
   ↓
classify         what is the deliverable, and on what surface is it verified?
   ↓
generate         Claude Opus, structured tool-use output (not regex parsing)
   ↓
criticize        four post-generation passes re-read the plan against the diff
                 and flag anything the code doesn't support
   ↓
plan             AC coverage matrix · grounding warnings · per-case provenance
                 → the web UI, a Jira comment, markdown, or JSON
```

The whole pipeline lives in
[`services/plan_service.py`](src/app/services/plan_service.py), and **every
caller goes through it** — web UI, CLI, MCP server and queue watcher — so the
same ticket produces the same plan whichever door it came in through.

## What makes the plans trustworthy

A plan that confidently tests behavior nobody shipped is worse than no plan.
Most of this repo is the machinery that stops that:

| | |
| --- | --- |
| **Four critics** | Fix-scope, AC-support, code-grounding and surface-mismatch passes re-read every case against the PR diff, the cited AC text and the repo. Unsupported cases are badged, never silently kept — and a fifth does the same for the regression checklist |
| **Per-case provenance** | Every case carries `grounded_in` (`PR:456`, `comments:123`, `Figma:abc`) and the AC IDs it covers. Cases with neither get an "Untraced" pill |
| **Coverage matrix** | ACs are extracted per ticket and every case tags what it exercises, so uncovered ACs and invented AC IDs both surface |
| **Loud refusals** | A throttled code search, a truncated response or an unreadable diff is reported as "couldn't check", not as "checked and clean" |
| **Shape rules** | Copy-only diffs get a capped checklist; risky diffs get API-level security negative tests; shared components get a per-role fan-out |
| **PII scrub** | Real customer and employee names from ticket context never become test subjects |

→ Full detail in **[docs/FEATURES.md](docs/FEATURES.md)**

## Quick start

**Prerequisites:** Python 3.11+, Node.js 20+, and `uv` (`pip install uv`).

```bash
# 1. Backend
uv sync
cp .env.example .env     # then fill in your tokens

# 2. Frontend
cd frontend && npm install && cd ..
```

Minimum `.env`: `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`,
`ANTHROPIC_API_KEY`, `MONGODB_URI`.
`GITHUB_TOKEN` is optional but strongly recommended — without it, plans see
only Jira data and never the code.

```bash
# 3. Run it — two terminals
uv run uvicorn src.app.main:app --reload    # → http://localhost:8000
cd frontend && npm run dev                  # → http://localhost:5173
```

→ Every setting, including the four that describe your team:
**[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**

## Four ways to use it

| | Best for | Start here |
| --- | --- | --- |
| **Web UI** | Day-to-day QA — ticket browser, workflow buttons, progress tracking | `http://localhost:5173` |
| **CLI** | Terminal-native and CI runs | [docs/CLI.md](docs/CLI.md) |
| **MCP server** | "Generate a test plan for PROJ-456" inside Claude Desktop | [docs/MCP_SERVER.md](docs/MCP_SERVER.md) |
| **Queue watcher** | Plans waiting *before* the tester opens the ticket | [docs/CLI.md](docs/CLI.md#qa-queue-watcher) |

The watcher is the one worth calling out: `testplan watch` sweeps your QA queue
and pre-generates plans (plus Bug Lens for bugs), so the multi-minute Opus run
happens off the tester's critical path. Unattended spend is fenced by a
merged-PR gate, a never-regenerate rule, a retry cooldown and a per-sweep cap.

```bash
testplan health                    # check API tokens
testplan generate PROJ-123         # generate a plan
testplan generate PROJ-123 --post-to-jira
testplan watch --dry-run --once    # show what a sweep would pick up
```

## Tech stack

**Backend:** Python, FastAPI, httpx, MongoDB (Motor) ·
**Frontend:** React, Vite ·
**LLM:** Claude API (Anthropic) or Ollama ·
**CLI:** Typer, Rich, PyYAML

## Project structure

| Path | What's in it |
| --- | --- |
| [`src/app/`](src/app/) | FastAPI routes, Jira/GitHub/Figma/Confluence clients, LLM integration |
| [`src/app/services/plan_service.py`](src/app/services/plan_service.py) | **The test-plan pipeline.** Every caller goes through here; also owns Jira-issue → prompt-payload assembly |
| [`src/app/services/test_plan_generator.py`](src/app/services/test_plan_generator.py) | Pipeline stages: AC coverage, warning normalization, the four critics |
| [`src/app/services/queue_watcher.py`](src/app/services/queue_watcher.py) | Sweeps the QA queue and pre-generates plans |
| [`src/app/copy_only.py`](src/app/copy_only.py) | Copy-only diff detection, its plan-shape rule, and the budget audit |
| [`src/app/security_surfaces.py`](src/app/security_surfaces.py) | Diff-driven risky-surface detection and the security negative-test block |
| [`src/cli/`](src/cli/) · [`src/mcp_server/`](src/mcp_server/) | CLI tool and MCP server |
| [`frontend/`](frontend/) | React web UI |
| [`tests/`](tests/) | Unit and integration tests |

## Testing

```bash
uv run pytest tests/ -q              # full suite
uv run python tests/run_tests.py     # unit tests
uv run python tests/test_llm.py      # LLM integration
```

Several suites pin a *refusal* rather than a happy path, because this codebase's
recurring bug is a transient failure that reads as a clean result:

| Suite | What it pins |
| --- | --- |
| `test_plan_service.py` | Every caller runs the same pipeline, field for field |
| `test_queue_watcher.py` | The guards that stop unattended sweeps overspending |
| `test_github_search_throttle.py` | A rate-limit 403 must not read as "no hits" |
| `test_bounce_in_prompts.py` | Bounce history reaches *both* prompt builders |
| `test_stored_artifact_reuse.py` | Stored plans are reused, not re-bought |

Run one with `uv run pytest tests/<file> -q`.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs pytest plus
frontend lint and build on every push and PR, and needs **no repository
secrets** — see [docs/CLI.md](docs/CLI.md#continuous-integration) for why.

## Deploying for a team

There is **no built-in authentication.** Before more than one person uses it:
put SSO or OAuth in front, restrict the network, serve over HTTPS, update CORS
in [`src/app/main.py`](src/app/main.py), and give each user their own tokens.
Watch Claude spend (pay-per-token) and the GitHub rate limit (5,000/hour).

Three shapes this takes:

1. **Personal use** — run locally with your own tokens
2. **Internal team** — internal server behind SSO + network restrictions
3. **Public SaaS** — needs multi-tenant architecture, encrypted token storage and billing

## Documentation

| | |
| --- | --- |
| [Features](docs/FEATURES.md) | Every capability in detail |
| [Configuration](docs/CONFIGURATION.md) | Every environment variable |
| [CLI & watcher](docs/CLI.md) | Terminal usage, unattended sweeps, CI |
| [HTTP API](docs/API.md) | Endpoint reference |
| [MCP server](docs/MCP_SERVER.md) | Claude Desktop setup and troubleshooting |
| [Roadmap & history](docs/ROADMAP.md) | Where it stands, what's next, what shipped |
| [Prompt improvements](docs/PROMPT_IMPROVEMENTS.md) | Why the prompt looks the way it does |
| [Testing complex features](docs/TESTING_GUIDE.md) | Getting good plans out of large tickets |

**Next on the automation track:** a jobs table so generation survives a
refresh, per-case results with evidence instead of a bare checked-set, and a
risk-ranked review checkpoint. See [docs/ROADMAP.md](docs/ROADMAP.md#next-up).

## License

MIT — see [LICENSE](LICENSE).
