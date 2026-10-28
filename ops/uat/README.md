# Unattended UAT runner: permissions

Design: [docs/design/uat-loop.md](../../docs/design/uat-loop.md).

The runner agent runs headless with an explicit allowlist and nothing else:

```sh
claude -p "<run prompt>" \
  --agent "$UAT_RUNNER_AGENT" \
  --permission-mode dontAsk --permission-prompts none \
  --settings ~/.config/jira-testplan-bot/uat-runner-settings.json \
  --output-format stream-json --verbose < /dev/null
```

started with `UAT_RUNNER_CWD` as the working directory: MCP servers configured
per project only load when the run starts inside that project.

`dontAsk` + `--permission-prompts none` means any tool call not allowed below is
**denied, never prompted**, and listed in the JSON result's
`permission_denials`. Nothing is bypassed. The runner has no tracker write
access at all: results go to the bot, and the bot posts after a human approves.

There is deliberately no `--max-budget-usd`: that flag stops the run, and dollar
budgets here only alert. The dispatcher reads cost from the stream as it goes
and alerts when a run or the day passes its budget; its wall-clock cap is the
only thing that stops a run.

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
* **Device and session preflight.** A booted simulator and a fresh web session
  are preconditions the dispatcher checks before claiming a run; the agent
  does not boot devices or log in.
