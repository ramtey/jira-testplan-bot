# UAT loop: from "In Testing" to reviewed results, with one human checkpoint

Status: approved design, delivered in phases (P0–P5 below).

Goal: a ticket enters testing, its plan is posted, the automatable cases run,
and pass/fail results plus a next step come back — with exactly one human
action, the QA reviewer confirming the results are right.

## Today

| # | Step | Code | Trigger | Failure behaviour |
|---|---|---|---|---|
| 1 | Plan pre-generated for the QA queue | `queue_watcher.sweep_once` → `plan_service.generate_single` | `testplan watch --once` on an interval, within the configured working hours | Per-ticket outcome (`screen_failed`, `fetch_failed`, `generate_failed`, `no_source`, `github_unreadable`); heartbeat on `/health/watcher` |
| 2 | Plan stored; progress key derived server-side | `run_tracker`, `progress_key.py`, `plan_revision` (`progress_remap`) | — | `not_persisted`; an empty plan raises |
| 3 | Plan auto-posted when the ticket enters testing | `workflow_routes.run_workflow_action` (button) or `queue_watcher.sweep_auto_post` (board move) → `auto_post.auto_post_own_plan` | Click, or next sweep | Skip / fail reasons stored in `auto_post_outcomes`; alert when an outcome changes |
| 4 | A person starts the runner agent | `GET /tickets/{key}/live-plan` | Manual | Distinguishes "no plan" from "store unreachable" |
| 5 | Agent runs cases, records a walkthrough | `GET /plans/{id}/video-walkthrough` | — | Not Run reasons exist only in the chat report |
| 6 | Agent marks passes | `GET /plans/{id}/progress-key`, `PUT /test-plan-progress/{key}` | — | Unknown ids → 422. **Who passed a case, and on what evidence, is not recorded** |
| 7 | Person reviews, posts, transitions | workflow routes (Pass to UAT / Fail back) | Clicks | `comment_posted: false` on a failed comment |

Defects found while mapping, fixed by this work:

* **Lost update.** The browser PUTs the whole checked set after each toggle and
  the server replaces it, so an agent write between a poll and a toggle is
  silently erased.
* The workflow routes hard-code one project key; the approval step calls them,
  so that moves to configuration.

## What an unattended runner may do

A headless run uses `claude -p --permission-mode dontAsk --permission-prompts none`
with an explicit allowlist. Nothing is bypassed: any tool call off the list is
denied (never prompted) and reported in the run's `permission_denials`.

Measured:

* An allowlisted MCP tool works, but only when the run starts in a directory
  where that MCP server is configured — the runner's working directory is
  configuration, not an accident.
* Bash that is not allowlisted is denied. A compound command (`a && b`) is
  checked part by part: it runs only if every part is allowed or read-only.
  Prefix rules with arguments work (`Bash(bash /abs/script.sh:*)`).
* Read-only Bash (`date`, `cat`) is auto-allowed.
* File writes are scoped with `Edit(//abs/path/**)` (it governs Write too);
  `Write(...)` path rules did not match. Inside the path is allowed, outside is
  denied.
* A whole MCP server can be allowed (`mcp__<server>`) with single tools
  denied; a denied tool is removed from the session, not just refused.
* Subagents inherit the allowlist: same allowed writes, same denials.
* A headless process waits 3 s for stdin unless it is redirected
  (`< /dev/null`).

Consequence: **the runner never writes to the tracker.** It may write only
(a) files under its evidence directory and (b) results through this repo's MCP
server. Everything that leaves the machine is posted by the backend after the
reviewer approves.

## Flow

1. **Trigger.** A run is queued when the ticket is in the testing status, its
   own plan is live in the tracker, and the deploy check says the build
   includes the change. The watcher sweep and the Pull-to-Testing route both
   queue (a DB write only). Unposted plans never run.
2. **Dispatch.** `testplan uat-dispatch --once` (scheduled, same working-hours
   window) claims one queued run atomically with a lease, preflights (backend
   routes present, DB, source host, device, web session), and starts the
   headless runner. Agent name, working directory, allowlist file and budgets
   are local configuration (`UAT_RUNNER_*`); the repo ships a template only.
   One run at a time: the simulator is a single shared device.
3. **Plan.** The run is pinned to `plan_id`, `plan_version` and `progress_key`
   at claim. `get_uat_run` returns the pinned cases from the same producer as
   `/live-plan`; video chapters come from `/plans/{id}/video-walkthrough`.
4. **Results.** `record_case_result` appends to `<evidence>/results.jsonl`
   first, then writes the DB; on a DB failure it answers `spooled` and the
   spool is replayed at finish. A pass with no evidence, or a Not Run without a
   reason code and detail, is refused.
5. **Checkpoint.** On finish the run is `awaiting_review` and a desktop alert
   fires. The review panel shows: run header (plan version and whether it is
   still live, build check, cost, duration, permission denials), failures with
   frame and video timestamp, agent passes with assertion and evidence, flaky
   passes badged, Not Run grouped by reason. Per-case overrides are recorded as
   results with the reviewer as actor. **Approve & post** carries the derived
   next step, editable.
6. **Tracker output — only after approval.** One results comment (own marker,
   single slot per ticket, edited in place on later runs): plan version, build,
   counts, failures with evidence names, "still needs a person", next step, and
   the plan's `how_to_see_it` as the video description. Attachments: the
   narrated walkthrough mp4 and the `FAIL-` frames. Then the chosen workflow
   transition. The formatter has one producer, run under node like the plan
   formatter.

## Data model

**`uat_runs`**: `ticket_key`, `plan_id`, `plan_version`, `progress_key`,
`issue_type`, `trigger`, `status`, `status_reason`, `status_detail`,
`claimed_at`, `lease_expires_at`, `heartbeat_at`,
`runner{agent, host, model, pid, session_id}`,
`build{source: deploy_state|ui_gate|unverified, entries}`,
`ticket_snapshot{updated, merged_pr_commits, description_hash}`,
`budget_usd`, `cost_usd`, `permission_denials[]`, `counts`,
`review{actor, at, decision, next_step, notes}`,
`tracker{comment_id, posted_at, attachments, transition, outcome}`.
Partial unique index: one active run per ticket.

Statuses: `queued`, `waiting_for_plan_post`, `waiting_for_build`, `claimed`,
`running`, `awaiting_review`, `approved`, `rejected`, `cancelled`,
`interrupted`, `failed`, `stale_plan`. None means "done" without a reason.

**`case_results`** (append-only): `run_id?`, `ticket_key`, `plan_id`,
`plan_version`, `case_id`, `case_title` (snapshot),
`status: passed|failed|not_run`, `not_run{code, detail}`,
`actor{kind: agent|human|legacy_unknown, name}`,
`evidence[]{kind: assertion|screenshot|video_ts|unit_test|http, ref, at_s, text}`,
`attempts[]`, `flaky`, `started_at`, `finished_at`, `recorded_at`,
`carried_from{plan_id, case_id}`.

Not Run codes: `missing_fixture`, `blocked_environment`, `wrong_build`,
`needs_client_token`, `deferred_to_ci`, `not_independently_verified`,
`not_automatable`, `dropped_time_cap`, `permission_denied`, `run_cancelled`,
`run_aborted`, `plan_changed`.

Results are keyed by `plan_id + case_id`, not by progress key, which removes
same-key regeneration misattachment for results. The browser still never
computes a key.

**Migration.** Browser toggles move to a per-case endpoint (one id, add or
remove), fixing the lost update. `checked_ids` stays as a server-maintained
projection — latest result is a human pass, an approved agent pass, or a
legacy mark — so carry-over, revision and other-shapes keep working. A one-shot
backfill records existing marks as `actor: legacy_unknown` (we cannot tell who
made them). Carry-over learns to copy result records, preserving the actor.
The human actor's name comes from `QA_ACTOR_NAME` (there is no login).

## Decisions

1. An agent pass counts as checked only after approval; before that it shows as
   "agent pass, pending review".
2. Approve also performs the transition, with the derived next step preselected.
3. Nothing is posted to the tracker before approval.
4. Runs trigger on entering the testing status, not on the ready queue.
5. One whole-case retry, for infrastructure-class errors only; assertion
   failures are never retried. A detour inside a case's steps is a FAIL.
6. Deploy-state sources should be configured before unattended runs; without
   them every run is marked "build: UI gate only".
7. The walkthrough is attached to the tracker on approval; external video
   hosts stay optional and manual.
8. Budgets (per-run dollars, per-run wall clock, per-day dollars) are a ceiling
   against runaway runs, set from P0 measurements.
9. `QA_ACTOR_NAME` names the human actor.
10. The repo ships the generic contract (MCP tools, allowlist template, a
    skeleton agent); the team's runner agent stays in local configuration.
11. Pre-existing marks are recorded as `legacy_unknown`.

## Edge cases

| Case | Handling → visible state |
|---|---|
| Plan regenerated mid-run | Results stay on the pinned plan → `stale_plan: superseded by vN`; review offers carry-over or rerun; posting refused while the pinned plan is not live |
| Ticket moved back to development | `should_continue` between cases → `cancelled: ticket moved to <status>`; remaining cases `run_cancelled` |
| Ticket moved on to UAT | `cancelled: overtaken`; results kept, nothing posted |
| Two runs for one ticket | Unique active index + atomic claim → `skipped: run N active` |
| Machine sleeps / network drops | Spool + replay; expired lease → `interrupted`; a rerun skips cases already resulted for that plan version |
| Device or browser crash | Agent records `run_aborted`; non-zero exit → `failed: runner exited <code>`; every case left without a result becomes `run_aborted` at finalize |
| Build not deployed | "not included" → `waiting_for_build`, rechecked each dispatch, alert after 4 h; "could not check" → `blocked: build unverifiable`; no sources configured → `build: ui_gate only` |
| Not automatable | `not_automatable` / `needs_client_token` with detail; listed as "still needs a person" |
| Flaky | `passed`, `flaky: true`, review required |
| Partial run | `partial`, reviewable, dropped cases in priority order |
| Run older than the latest plan | As regeneration: posting blocked |
| Plan never posted | `waiting_for_plan_post` with the auto-post reason |
| Tracker API failure | Retry transport; single-slot comment edited in place; "comment landed, transition failed" is its own state |
| Ticket changed while running | Compared with `ticket_snapshot` at finish → `build drifted` or "description changed" flags |
| No merged PR | No plan, no run; a hand-generated plan on an open PR → `blocked: no merged PR` |
| Bug vs story | Same flow; `issue_type` stored so later bug-filing can tell "fix failed" from "new regression" |
| Oversized plan | Runner reads the store; an oversized results comment becomes summary + attachment |
| Mobile vs web | Routed per case by `surface`; device locked per run; web cases may run in parallel |
| Cost / runtime | `--max-budget-usd`, dispatcher wall-clock cap (SIGINT first so recordings finalize), daily cap; cost stored per run |
| Browser overwrites agent marks | Per-case endpoint |
| Permission denied mid-run | `not_run: permission_denied <tool>`; run lists denials; no workaround |
| Pass without evidence | 422 |
| Backend predates the routes | Preflight fails with "restart the backend" |
| Human ticks during a run | Both kept with actors; disagreements highlighted |
| Window closing | Not started if it cannot finish before working hours end |
| Web session expired | Allowlisted refresh; if it needs MFA, web cases `blocked_environment: auth` |

## Phases

| Phase | Ships | Verified by |
|---|---|---|
| P0 Spike | Allowlist template, headless-mode contract for the runner agent, measured cost and time on real tickets in dry mode | Denials limited to the expected ones; subagent permission inheritance and Write path scoping known |
| P1 Results model | `case_results`, projection, per-case toggle, backfill, lost-update fix | A UI tick produces a human result; old marks unchanged |
| P2 Runner writes | MCP tools (`get_uat_run`, `record_case_result`, `should_continue`, `finish_run`), spool/replay, `testplan uat-run KEY` | Two tickets run by command; a forced DB outage mid-run loses nothing |
| P3 Checkpoint + tracker | Review panel, Approve & post, results formatter, mp4 attachment, transition, project key from config | Two tickets approved; comment, video, transition in the tracker |
| P4 Unattended | Queue, scheduled dispatcher, lease, cancel on move, deploy gate, budgets, alerts, health pill | A board move ends in a run awaiting review with nothing typed |
| P5 Edge matrix | A test per edge-case row; three forced failures on real runs | Each produces its named state |

## Definition of done

* 5 real tickets end to end with no human action except the checkpoint —
  at least one mobile, one web, one bug, one with a real failure or Not Run.
* On each: results comment, video and transition in the tracker, posted only
  after approval.
* Every case in every run ends with a result or a reason code (enforced by
  `finish_run`).
* Zero runner writes to the tracker (none allowlisted; denials reviewed).
* A test per edge-case row; the P5 forced failures each show their state.
* Cost and duration recorded on every run; median testing-entry → awaiting
  review ≤ 45 min across the 5.
* Simultaneous browser and agent writes lose nothing.
* No team names, keys or URLs in the repo.
