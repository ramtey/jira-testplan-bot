# CLI and the QA-queue watcher

The CLI provides a fast, terminal-native way to generate test plans without
running the web server — and hosts `testplan watch`, the background sweep that
pre-generates plans for the QA queue.

## Installation

**For teams** (one-liner install):
```bash
curl -sSL https://raw.githubusercontent.com/ramtey/jira-testplan-bot/main/install.sh | bash
```

**Or install directly** (if you have `uv`):
```bash
uv tool install git+https://github.com/ramtey/jira-testplan-bot.git
```

**For local development**:
```bash
git clone https://github.com/ramtey/jira-testplan-bot.git
cd jira-testplan-bot
uv sync
uv run testplan --help
```

**Update later**: `uv tool upgrade testplan`

## Configuration

**Interactive setup** (recommended):
```bash
testplan setup
```

**Import from .env file**:
```bash
testplan config import .env
```

**Or use environment variables** (for CI/CD):
```bash
export JIRA_URL="https://your-company.atlassian.net"
export JIRA_USERNAME="your-email@company.com"
export JIRA_API_TOKEN="your-token"
export ANTHROPIC_API_KEY="sk-ant-api03-..."
export GITHUB_TOKEN="ghp_..."  # optional
export FIGMA_TOKEN="figd_..."  # optional
```

Config is stored at `~/.config/jira-testplan/config.yaml` with environment variable fallback.

## Usage

```bash
# Check API token health
testplan health

# Generate test plan
testplan generate PROJ-123

# Post directly to Jira
testplan generate PROJ-123 --post-to-jira

# Save to file or copy to clipboard
testplan generate PROJ-123 -o plan.md
testplan generate PROJ-123 --copy

# Batch processing
testplan generate PROJ-123 PROJ-124 PROJ-125

# Output formats: markdown (default), jira, json
testplan generate PROJ-123 --format json

# Pre-generate plans for everything sitting in the QA queue
testplan watch --dry-run --once   # show what a sweep would pick up
testplan watch --once             # one sweep, then exit
testplan watch                    # loop on the configured interval
```

## QA-queue watcher

`testplan watch` sweeps the configured Jira projects for tickets in the
queue status (default `Ready to Test`) and pre-generates a test plan for
any that don't have one — plus Bug Lens for Bug tickets. The point is lead
time: the tester opens a ticket whose plan, critic verdicts and analysis
are already waiting, instead of starting a multi-minute Opus run at the
moment they wanted to start testing. Pull-to-Testing already auto-generates,
but only *after* the click, which puts the whole latency on the critical path.

It runs as its own process, never from the API — so it can be restarted
independently and never spends money just because the backend is up.

Every sweep makes real Opus calls with nobody watching, so each guard is
separately configurable (all via env):

| Setting | Default | What it does |
| --- | --- | --- |
| `WATCH_PROJECTS` | falls back to `WORKFLOW_PROJECT_PREFIXES` | Projects to sweep |
| `WATCH_STATUS` | `Ready to Test` | The status that means "in the QA queue" |
| `WATCH_INTERVAL_SECONDS` | `300` | Seconds between sweeps |
| `WATCH_MAX_PER_CYCLE` | `5` | Cap on plans per sweep — bounds a surprise when a sprint's worth of tickets moves at once |
| `WATCH_REQUIRE_MERGED_PR` | `true` | Require at least one **merged** PR. Merge state, not just existence — a plan written against an open PR describes code that is still changing, and the watcher never regenerates, so that plan would outlive the code it came from |
| `WATCH_RETRY_COOLDOWN_HOURS` | `6` | Don't re-attempt a ticket attempted this recently, so one that fails every cycle doesn't burn a call every interval |

**What the tester sees.** Opening a watcher-prepared ticket loads the stored
plan as the live plan, and a stored Bug Lens analysis with it. Pull-to-Testing
still auto-generates only when no run exists, so it correctly reuses the
watcher's plan rather than paying twice — and now *shows* it, instead of
leaving a collapsed history banner where the plan used to appear. The banner
keeps its real job: older versions.

**Held tickets are always skipped**, whatever the hold reason. A hold is a
human saying the ticket isn't ready to be worked on, and `code-review`
literally means the PR is still in review — a plan written then describes
code that will change, and never-regenerate would make it permanent.
Skipping costs nothing: the ticket stays in the watch status, so the sweep
after the hold clears still writes the plan before the tester opens it.

A ticket that already has a stored plan is never regenerated — regeneration
stays a human decision. Checks run cheapest-first: the DB dedupe is free,
the Jira fetch costs a few hundred ms, and only what survives both reaches
the LLM. `--dry-run` runs every guard and stops short of generating, so the
skips it reports are the ones a real sweep would produce.

One watcher process is assumed; the dedupe is a read rather than a claim, so
two would race. A claim column (following
`jira_tickets.auto_bug_analysis_dispatched_at`) is the fix before this is
deployed anywhere shared.

#### Running it unattended (macOS)

A sweep is only useful if it happens without you. `ops/launchd/` installs the
watcher as a user LaunchAgent that runs one sweep every 5 minutes:

```bash
./ops/launchd/install.sh     # load it (idempotent — re-run after editing the template)
./ops/launchd/uninstall.sh   # stop and remove it
tail -f ~/Library/Logs/jira-testplan-watch.log
```

Each launch runs `testplan watch --once --quiet` rather than the CLI's own
loop, so nothing stays resident and launchd restarts a crashed sweep for free.
A shorter interval buys nothing: plans are never regenerated and a retry
cooldown applies, so cost tracks tickets entering the queue, not how often we
look.

Two limits worth knowing. `StartInterval` does not fire while the Mac is
asleep — launchd runs a single catch-up sweep on wake — so this is a
convenience, not infrastructure. And the sweep needs `MONGODB_URI`, which
settings read from `.env` relative to the working directory; that is why the
runner `cd`s into the repo before anything else.

## Continuous integration

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every push to
`main` and every pull request: `pytest` for the backend, `npm run lint` plus
`npm run build` for the frontend.

It needs **no repository secrets**. The suite is hermetic — no network, no real
credentials — with one wrinkle: `init_client` refuses to guess a database, so a
handful of tests fail without `MONGODB_URI` even though every query is mocked.
CI sets a throwaway URI, which satisfies the check without a database — Motor
connects lazily, so nothing dials out. The repository tests go further and run
against an in-memory Mongo (`mongomock-motor`), so the query layer is exercised
for real without CI needing a server. Keep it that way; this repo is public.

`pyproject.toml` pins `testpaths = ["tests"]`. Without it a bare `pytest`
walks `src/` and tries to import `test_plan_progress` and
`test_plan_generator` — domain modules about test *plans*, not tests — as test
modules, which aborts collection before a single test runs.

## CLI in CI/CD

The CLI supports environment variables for automation. Example GitHub Actions workflow:

```yaml
- name: Generate test plan
  run: testplan generate $TICKET --post-to-jira
  env:
    JIRA_URL: ${{ secrets.JIRA_URL }}
    JIRA_USERNAME: ${{ secrets.JIRA_USERNAME }}
    JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```
