# Unattended UAT runner: permissions

Design: [docs/design/uat-loop.md](../../docs/design/uat-loop.md).

Start a run with:

```sh
testplan uat-run PROJ-123            # every leg
testplan uat-run PROJ-123 --route-only   # pin the plan, check builds, route cases; launch nothing
testplan uat-run PROJ-123 --run 42   # resume: replay spooled results, close or launch legs
```

The backend pins the run to the ticket's live plan, answers the build question
for each repo the ticket changed, and routes every case to a **runner profile**
(`UAT_RUNNER_PROFILES_FILE`, see `runner-profiles.example.json`) or records why
none can run it. For each leg the command then starts that profile's agent
headless with its allowlist and nothing else:

```sh
claude -p "<run prompt>" --agent <profile.agent> \
  --permission-mode dontAsk --permission-prompts none \
  --settings <profile.settings> \
  --mcp-config <evidence>/mcp-<leg>.json --allowedTools mcp__testplan-uat \
  --output-format stream-json --verbose < /dev/null
```

in the profile's `cwd`: MCP servers configured per project only load when the
run starts inside that project.

`dontAsk` + `--permission-prompts none` means any tool call not allowed is
**denied, never prompted**, and listed in the JSON result's
`permission_denials`. Nothing is bypassed. The runner has no tracker write
access at all: results go to the bot through the `testplan-uat` MCP server
(`testplan-uat-mcp`, bound by its environment to one run and leg), and the bot
posts after a human approves.

The `testplan-uat` tools: `get_uat_run` (the leg's cases, builds, results so
far), `record_case_result`, `should_continue`, `finish_run` (lists cases still
owed), and `http_request` (only to the profile's `http_allowed_hosts`; request
and response saved under the evidence directory with secrets redacted).
Every result is written to `<evidence>/results.jsonl` before it is sent; if the
backend is down the tool answers `spooled`, and `finish_run` and the command
replay it. The backend dedupes on the result's `client_id`.

There is deliberately no `--max-budget-usd`: that flag stops the run, and dollar
budgets here only alert (`UAT_RUN_BUDGET_USD`). The wall-clock cap
(`UAT_LEG_WALL_CLOCK_MIN`) is the only thing that stops a leg: SIGINT first so
a recording can finalize, then unreached cases are `dropped_time_cap`.

## Making the local settings file

Copy `runner-settings.template.json` to
`~/.config/jira-testplan-bot/uat-runner-settings.json` and replace:

| Placeholder | Value |
|---|---|
| `__EVIDENCE_ROOT__` | Absolute path of the evidence directory, e.g. `/Users/you/uat-walkthroughs`. The rule becomes `Edit(//Users/you/uat-walkthroughs/**)`; the double slash is what makes it absolute. |
| `__RUNNER_SCRIPTS__` | Absolute path of the runner agent's `scripts/` directory. |

Add the MCP server names your team uses if they differ from `ios-simulator`
and `playwright`, and any app-specific commands you are willing to run
unattended (a unit-test task, a session-refresh task). Keep each as a fixed
prefix, never a bare interpreter (`node`, `python3`, `ffmpeg`), because a bare
interpreter can write anywhere and would undo the `Edit` scoping.

## Rule behaviour (measured)

* `Edit(//abs/path/**)` scopes the Write tool too; `Write(...)` path rules did
  not match.
* `mcp__<server>` allows a whole server; a `deny` entry for one of its tools
  removes that tool from the session.
* Bash is checked part by part: `a && b` runs only if every part is allowed or
  read-only. Read-only commands (`cat`, `date`, `ls`) need no entry.
* Subagents the runner spawns inherit the same allowlist.
* Redirect stdin (`< /dev/null`) or the process waits 3 s for it.

## Known gaps (to be closed before unattended runs)

* **Web walkthrough recording** needs a Playwright script with `recordVideo`,
  which the agent writes and runs under `node`. A bare `node` cannot be
  allowlisted safely. Until a fixed driver exists (steps as JSON in, video
  out), the web walkthrough is recorded as Not Run `permission_denied`.
* **Recording control.** Stopping a simulator recording needs `kill -INT
  <pid>`, which is not safe to allowlist; `sim-record.sh start|stop` owns the
  pid instead. Video normalizing, contact sheets and loudness checks go through
  `video-tools.sh` for the same reason (a bare `ffmpeg` can overwrite any file).
* ~~**Build check.**~~ Closed: the bot answers it per repo at run start
  (`deploy_state` with `UAT_TEST_ENV`), and cases whose build is not in are
  never handed to the runner. Repos with no deploy source are `unverified`:
  the runner's UI gate is the only check.
* ~~**Bot reads.**~~ Closed: `get_uat_run` carries the cases, builds and video
  chapters.
* **Request-level cases**: `http_request` closes the host allowlist and the
  transcript. Named token profiles are still open: a case that needs a bearer
  token the runner cannot obtain is `needs_client_token`.
* **Server-side observations** (headers a service sends upstream, traces) are
  `not_automatable` for a runner; they stay with a person.
* **Device and session preflight.** A booted simulator and a fresh web session
  are preconditions the dispatcher checks before claiming a run; the agent
  does not boot devices or log in.
