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

1. **Trigger.** A run is queued when the ticket is in the testing status and
   its own plan is live in the tracker. The watcher sweep and the
   Pull-to-Testing route both queue (a DB write only). Unposted plans never
   run. Whether each case may run yet is decided per case (see "Runner profiles
   and multi-repo tickets").
2. **Dispatch.** `testplan uat-dispatch --once` (scheduled, same working-hours
   window) splits the run into legs, one per runner profile its cases need,
   claims a leg atomically with a lease, preflights (backend routes present,
   DB, source host, device, web session), and starts that leg's headless
   runner. Profiles, allowlist files and budgets are local configuration; the
   repo ships a template only. One leg per device at a time: the simulator is
   a single shared device.
3. **Plan.** The run is pinned to `plan_id`, `plan_version` and `progress_key`
   at claim. `get_uat_run` returns an index of the leg's cases (id, title,
   surface, each repo's build status, any result so far), the video chapters
   and the hosts `http_request` may call; `get_case` returns one case in full.
   Both come from the same producer as `/live-plan`. (The full view of a large
   plan does not fit in one tool result, hence the index.)
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

## Runner profiles and multi-repo tickets

A board is not an app. One project's tickets can change a mobile app, a web
app, a backend, or several at once, and a runner only works from the directory
where its app's tools are configured. So the runner is chosen per repo, not per
project or per ticket.

* **Runner profile** (local configuration, keyed by repo like
  `DEPLOY_STATE_SOURCES`): agent, working directory, allowlist file, surfaces
  it covers, app identity (bundle id or base URL). Repos with no profile are
  named, never guessed.
* **Per-case routing.** The plan already records one build target per repo
  (own merge commit, platform, app version) and which repos each case exercises
  (`build_targets.targets_for_case`). A case's `surface` decides **who runs
  it**; its repos decide **when it can run**: only once every repo it
  exercises is in the deployed build. A case that drives the mobile app against
  a new backend endpoint runs on the mobile leg and waits for both builds.
* **Legs.** One ticket is one run, one review and one tracker comment. Inside
  it, each runner profile the cases need is a leg with its own process, working
  directory, lease, cost and permission denials. All legs record results on the
  same run.
* **Partial deploys.** Cases whose builds are not in yet wait as
  `waiting_for_build (<repo>)`; when the build lands, only those cases run, on
  the same plan version, and results accumulate. The review opens when every
  case has a result, or after one working day with the waiting cases listed by
  repo.
* **Named gaps.** A repo without a profile → its cases are
  `no_runner_profile (<repo>)` and the rest of the ticket runs. A repo whose PR
  is not merged → `pr_not_merged (<repo>#n)`.
* **Tracker output** groups results by repo with each repo's build status, so
  a developer sees which side failed.

Grouping several tickets into one plan and run is separate work; legs carry
over to it unchanged.

## Data model

**`uat_runs`**: `ticket_key`, `plan_id`, `plan_version`, `progress_key`,
`issue_type`, `trigger`, `status`, `status_reason`, `status_detail`,
`claimed_at`, `lease_expires_at`, `heartbeat_at`,
`legs[]{profile, repos, surfaces, status, claimed_at, lease_expires_at,
heartbeat_at, runner{agent, host, model, pid, session_id}, cost_usd,
permission_denials[]}`,
`builds[]{repo, merge_commit, status: included|not_included|unverifiable,
source: deploy_state|ui_gate|unverified, checked_at}`,
`ticket_snapshot{updated, merged_pr_commits, description_hash}`,
`budget_usd`, `cost_usd` (sum of legs), `counts`,
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
`leg`, `builds_checked[]{repo, merge_commit, status}`,
`carried_from{plan_id, case_id}`.

Not Run codes: `missing_fixture`, `blocked_environment`, `wrong_build`,
`needs_client_token`, `deferred_to_ci`, `not_independently_verified`,
`not_automatable`, `dropped_time_cap`, `permission_denied`, `run_cancelled`,
`run_aborted`, `plan_changed`, `waiting_for_build`, `no_runner_profile`,
`pr_not_merged`.

Every result records both the progress key it is filed under and the
`plan_id` it was read from. The checked set is still derived per progress key,
so regeneration behaves as before; the review panel (P3) reads by `plan_id`,
which is what ends same-key misattachment there. The browser still never
computes a key.

**Migration (shipped in P1).** Browser toggles go to a per-case endpoint
(`POST /plans/{id}/results`, one id, passed or cleared), which ends the lost
update. The checked set is derived on read: the old progress row is a frozen
baseline and results are applied on top in order, so carry-over, revision and
other-shapes read through the same call unchanged. There is no backfill job:
a baseline mark with no newer result is reported as `legacy_unknown`, which is
what a backfill would have written. The whole-set `PUT` remains for scripts and
records its difference as `legacy_unknown` results. Carry-over records the
tester's own passes. The human actor is the `users` row named by
`QA_ACTOR_EMAIL` (there is no login).

## Decisions

1. An agent pass counts as checked only after approval; before that it shows as
   "agent pass, pending review".
2. Approve also performs the transition, with the derived next step preselected.
3. Nothing is posted to the tracker before approval.
4. Runs trigger on entering the testing status, not on the ready queue.
5. One whole-case retry, for infrastructure-class errors only; assertion
   failures are never retried. A detour inside a case's steps is a FAIL.
6. Deploy-state sources must cover every repo a runner profile tests before
   unattended runs; a repo without one is marked "build: UI gate only" on its
   cases.
7. The walkthrough is attached to the tracker on approval; external video
   hosts stay optional and manual.
8. Dollar budgets (per run, per day) **alert and never stop a run**; values come
   from P0 measurements. The per-run wall-clock cap is a real stop, because it is
   what frees the device from a hung run: SIGINT first so the recording
   finalizes, then unfinished cases become `dropped_time_cap` and the run is
   reviewable as `partial`.
9. The human actor is a `users` row (the table runs already link to), chosen by
   `QA_ACTOR_EMAIL`. There is no login while the app runs locally for one
   person; when it is hosted, a login becomes required and only changes where
   the email comes from — no data migration.
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
| Build not deployed (per repo, per case) | "not included" → `waiting_for_build`, rechecked each dispatch, alert after 4 h; "could not check" → `blocked: build unverifiable`; no sources configured → `build: ui_gate only` |
| Ticket spans several repos | One run, one leg per runner profile; each case waits only for the repos it exercises; review opens when all cases have results or after one working day |
| Repo with no runner profile | Its cases `no_runner_profile (<repo>)`; other legs run |
| One repo's PR not merged | Its cases `pr_not_merged (<repo>#n)`; other repos' cases run |
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
| Cost / runtime | Dollar budgets alert (per run, per day) and never stop a run; dispatcher wall-clock cap stops it (SIGINT first so recordings finalize); cost stored per run |
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
| P2 Runner writes | MCP tools (`get_uat_run`, `get_case`, `record_case_result`, `should_continue`, `finish_run`, `http_request`), spool/replay, `testplan uat-run KEY`, build answer per repo, runner profiles and legs | Two tickets run by command; a forced DB outage mid-run loses nothing |
| P3 Checkpoint + tracker | Review panel, Approve & post, results formatter, mp4 attachment, transition, project key from config | Two tickets approved; comment, video, transition in the tracker |
| P4 Unattended | Queue, scheduled dispatcher, lease, cancel on move, deploy gate, budgets, alerts, health pill | A board move ends in a run awaiting review with nothing typed |
| P5 Edge matrix | A test per edge-case row; three forced failures on real runs | Each produces its named state |

### P2 as shipped

* `testplan uat-run KEY` asks the backend to start a run, then runs each leg's
  agent headless with `--mcp-config` (the `testplan-uat` server, bound to one
  run and leg by its environment) and `--allowedTools mcp__testplan-uat` on top
  of the profile's allowlist. `--route-only` stops after routing; `--run ID`
  resumes (replays the spool, closes legs whose runner already ran).
* The build answer is read at run start for every repo the ticket changed,
  against `UAT_TEST_ENV` (an ordered list, since repos name the same
  environment differently). Deploy sources come from `DEPLOY_STATE_SOURCES`
  or from the deployments repos (`DEPLOYMENTS_REPOS`); a repo found in
  neither is `unverified`.
* Cases that cannot run get their reason from the router at start
  (`no_runner_profile`, `waiting_for_build`, `pr_not_merged`,
  `blocked_environment` for an unreadable build, `not_automatable` for
  manual-only), so the review shows them from the first minute.
* Measured on a 49-case backend ticket: one leg, 2.7 min, $1.69, every case
  accounted for, two permission denials (both read-only exploration). The
  backend was stopped for 25 s mid-run: 9 results spooled and were replayed
  once each, no duplicates. Most cases were Not Run because the build source
  was not configured and the runner was not told which host it could call;
  both are fixed (`DEPLOYMENTS_REPOS`, `http_allowed_hosts` in the index).
* Still open for later phases: rerunning a leg whose runner died (P4),
  named token profiles for request-level cases.

### P3 as shipped

* The review panel sits above the plan on the ticket page and shows the
  ticket's latest run: header (plan version, cost, duration, permission
  denials), builds per repo, failures, runner passes pending review, passes by
  a person, Not Run grouped by reason (split into "still needs a person" and
  blocked), the video and `FAIL-` frames that will be attached, and a preview
  of the comment. A stale pinned plan blocks posting with the live version
  named.
* Overrides (`POST /uat-runs/{id}/review/results`) are results with the
  reviewer as actor and `source: review`; a failure needs a note. They close
  when the run is approved, so the posted comment matches the results.
* Next step is derived: any failure → fail back to In Progress; any Not Run →
  stay in testing; all passed → Pass to UAT. The reviewer can change it.
* Approve records the decision, marks the run's agent results
  `review_approved`, then attachments → results comment → transition, saving
  each step on `tracker`. A failed step is `attachments_failed`,
  `comment_failed` or `transition_failed`; approving again resumes without
  re-uploading or moving twice. The comment has its own marker
  (`🤖 UAT run results`), one per ticket, edited in place, and is excluded
  from plan-generation context. The transition reuses the workflow buttons'
  transition and assignee logic without their pass/fail comment, and the
  project gate is `WORKFLOW_PROJECT_PREFIXES` in the backend too.
* The formatter is `frontend/src/utils/uatResults.js`, run under node for
  posting, so the preview is the posted text.
* The walkthrough gate of the Pass to UAT button is not applied to approval:
  the results comment carries its own video line.

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
