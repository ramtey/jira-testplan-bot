# Features

Everything the bot does, in detail. For a short tour, see the
[README](../README.md).

- [Usage tips](#usage-tips)
- [Context gathering](#context-gathering)
- [Development integration](#development-integration)
- [Test plan generation](#test-plan-generation)
- [Plan-quality rules](#plan-quality-rules)
- [Web UI](#web-ui)
- [Jira browser side rail](#jira-browser-side-rail)
- [QA workflow actions](#qa-workflow-actions)
- [Jira Bug Lens](#jira-bug-lens)
- [Test plan history](#test-plan-history)
- [Parent ticket context](#parent-ticket-context)
- [Linked ticket dependencies](#linked-ticket-dependencies)

## Usage tips

- **Automatic generation**: Just enter a ticket key - the system fetches all context automatically
- **Multi-ticket mode**: Enter comma-separated keys (`PROJ-123, PROJ-456`) to combine related tickets into one plan; tickets must share a repository or overlapping changed files
- **Bug Lens**: For `Bug` tickets, an "Analyze Bug" button appears alongside "Generate Test Plan" — use it to get root cause, fix explanation, and regression tests grounded in the actual PR diff
- **Sub-tasks get parent context**: Design specs (Figma, images) from parent Epics/Stories are automatically included
- **Export formats**: Use Jira format for comments, Markdown for GitHub/Slack, JSON for programmatic use
- **GitHub token recommended**: Adds project-specific terminology and implementation details to test plans

## Context gathering

- **Automatic context gathering**: Fetches ticket details, PRs, commits, code changes, and repository docs
- **Parent ticket awareness**: Automatically fetches parent Epic/Story context for sub-tasks, including:
  - Parent descriptions and business requirements
  - Figma designs attached to parent tickets
  - Design mockups/screenshots from parent
  - Overall feature context that sub-tasks lack
- **Linked ticket dependencies**: Automatically fetches blocking and dependency relationships:
  - Issues this ticket blocks (test thoroughly - others depend on this)
  - Issues blocking this ticket (prerequisites that must be resolved first)
  - Root cause issues (for bugs - ensures actual cause is fixed)
  - Downstream issues this ticket may cause (validate no regressions)
- **Smart comment analysis**: Extracts testing-related Jira comments (test scenarios, edge cases, QA discussions)
- **QA/UAT bounce-back history**: Walks the issue changelog for transitions where the ticket reached an advanced state (QA / UAT / Testing / Ready-for-*) and was sent back to To Do, Backlog, Open, Reopened, or In Progress. Reason pairing is two-tier — first the nearest Jira comment within ±6 hours of the transition (slight bonus when authors match), then a fallback that walks the comments posted between when the ticket entered its reviewed state and the bounce, preferring non-dev voices, so older QA/UAT feedback that never got resolved is picked up instead of the dev's "will check" reply. Surfaced to the LLM as a "PRIOR QA / UAT BOUNCE-BACK HISTORY" section that asks for explicit regression coverage of each prior failure mode. In the UI, each bounce card leads with a one-sentence LLM headline (via `POST /bounce/summarize`) and a plain-English transition line ("Kyle moved this back to In Progress from Ready for UAT") with the raw comment tucked behind a "Show full comment" toggle; long comments are trimmed at paragraph / sentence / word boundaries with an ellipsis instead of a hard mid-word cut. Each card is **paired with the PR that shipped its fix** — the earliest PR merged after that specific bounce is shown with a link, merge time, and the changed files (+/− counts, 6 shown by default) — so older bounces pair with earlier fix PRs rather than every card crediting the latest merge
- **Figma integration**: Extracts actual UI component names from design files for specific test cases
- **Smart filtering**: Focuses on runtime behavior, ignoring build-time configs (ESLint, TypeScript, etc.)
- **Priority ordering**: Critical tests first, edge cases last

## Development integration

- **GitHub enrichment**: PR code diffs (actual source changes injected into LLM context), review comments, and repository documentation
- **Simulator test context**: Automatically pulls testID references and screen guides from `.agents/skills/simulator-testing/references/` in the target repo (when present), so Claude references real UI test IDs in generated test steps
- **Jira development data**: Commits, branches, and PR statuses with clickable links; merged PRs additionally show the merge date next to repo/author in the Development Activity card
- **Open-PR handling**: Open (un-merged) PRs are included in the LLM prompt and flagged as open in the UI header so QA can plan coverage for code that hasn't merged yet
- **Token health monitoring**: Real-time validation with expiration warnings

## Test plan generation

- **Claude Opus**: Defaults to `claude-opus-5`. The default and the per-model request quirks live together in [`model_capabilities.py`](../src/app/model_capabilities.py), so `LLM_MODEL` is the only knob to turn: `temperature` is dropped on models that reject it (Opus 4.7+), `output_config.effort` is sent only where it's accepted, and `max_tokens` reserves headroom for thinking on models that think by default (Opus 5+) — an unrecognised model id falls back to the conservative shape rather than a 400. Read timeout is configurable via `CLAUDE_API_TIMEOUT_SECONDS` (default 600s) so worst-case parents with many subtasks survive Opus's 16k-token output cap. Transient `529` overload errors from the plain-summary path are retried with exponential backoff so a brief Anthropic capacity blip no longer drops the ticket summary
- **One pipeline for every caller**: the deliverable classifier, the four post-generation critics, AC coverage and run persistence live in `src/app/services/plan_service.py`, and the web UI, CLI, MCP server and queue watcher all go through it. Previously each non-browser caller assembled its own context and called the LLM directly, so an MCP- or CLI-generated plan silently carried no critic badges, no AC coverage and no run history — the same ticket produced a different plan depending on which door you came in through
- **Smart comment management**: Updates existing Jira comments instead of creating duplicates
- **Multiple export formats**: Markdown, Jira-formatted text, or JSON. The markdown export includes superseded ACs and any grounding warnings so reviewers see the same caveats they would in the UI
- **Issue type validation**: Generates plans for Story, Bug, Task, and Sub-task; skips Epics and Spikes (Epics open the children view instead)
- **Epic launcher view**: Fetching an Epic renders its child tickets as a list with per-row Generate (test plan) and Analyze (Bug Lens) buttons; results expand inline so multiple children can be reviewed without navigating away
- **Multi-ticket AC coverage**: For comma-separated multi-ticket plans, ACs are extracted per ticket, fed to the LLM as a coverage matrix, and each test case must tag the AC IDs it covers. The UI shows per-ticket coverage ratios, lists uncovered ACs, and surfaces a red banner if the model invents AC IDs that don't exist. When two tickets disagree on an AC, the newer ticket's version wins and the older AC is marked superseded
- **Cross-project multi-ticket plans**: When the supplied tickets span multiple repositories, a seam extractor walks each PR diff for HTTP routes, events, and in-house imports, intersects exports/calls across repos, and feeds the resulting verified + suspected seams to the LLM so it emits real integration tests at the boundary. Cross-project test cases are badged with a producer → consumer line and the same metadata flows into the markdown export
- **No silent truncation**: Multi-ticket plans detect when Claude hits the max-tokens cap and surface the truncation explicitly instead of returning a partial plan
- **UI element grounding**: Test steps that name a UI element not present in the PR diff or the target repo's simulator `testID` reference are flagged in the rendered plan so QA can sanity-check the wording before running them
- **Fix-scope critic**: A post-generation pass (`src/app/fix_scope_critic.py`) snapshots each merged PR (title, body, files changed, key diffs, commit messages) and pairs every test case with its cited ACs; the LLM verifies whether the case asserts behaviour the PR actually changed, and unsupported cases are badged with `needs_manual_verification=True` plus a grounding warning. Catches reporter-diagnostic drift — the classic case is a bug ticket speculating in prose about a default rate the PR body explicitly said it wasn't touching, yet QA still gets an edge case asserting the rate is not auto-applied. The generator prompt also carries a "do not mistake the reporter's diagnosis for the fix's scope" block so the model prefers not to emit these in the first place
- **AC-support critic**: A second post-generation pass pairs each case's (title, steps, expected) with the verbatim text of every AC it cites in `covers_acs` and asks the LLM whether the AC actually supports the behaviour being tested. Cases the critic marks ungrounded are badged with `needs_manual_verification` and gain a grounding-warnings entry, so a case citing "audit history is viewable in the admin dashboard" for an assertion about **date-range filtering** shows up under the existing "Unverified UI" chip instead of reading as a scope gap
- **Code-grounding recheck**: Third-pass critic that searches each linked GitHub repo for the case's title, feeds the snippets + case body back to the LLM, and flips confirmed warnings from WARN to INFO with a `code_evidence` anchor. Fixes the false-positive class where the AC text is silent about an implementation detail (empty-buffer guard, streaming latency, cache invalidation) but the code actually implements it. Gated on `GITHUB_TOKEN` + `code_grounding_recheck_enabled`; failure degrades to leaving warnings at WARN. The frontend banner splits by severity — WARN for unconfirmed behaviour, INFO for cases the recheck confirmed in code with file-path anchors QA can jump straight to
- **Verification-surface anchoring**: Two gated passes (`SURFACE_CLASSIFIER_ENABLED`) keep plans on the ticket's actual deliverable surface — App Store Connect uploads, LaunchDarkly flag flips, doc rewrites, etc. — instead of always defaulting to "launch the app and compare". A pre-plan classifier names the deliverable + verification surface and injects anchor + off-target hints into the generator prompt so cases author against the right target the first time; a post-plan surface-mismatch critic badges cases whose steps still drift onto the wrong surface (same "Unverified UI" chip as the other critics, `source=critic_surface`). Multi-ticket batches classify each ticket in parallel; the surface critic skips entirely when a batch mixes code_behavior with non-code work so a legitimate "launch the app" step on the code ticket doesn't false-positive
- **Platform-scope rule**: Expo/React Native tickets no longer get auto-generated "App launches on Android emulator" smoke items when the ticket only discussed the Expo layer generically. The prompt requires an explicit platform mention (ticket, ACs, comments, PR, or diff) before a case names a platform, with a platform-neutral fallback when scope is ambiguous
- **Sibling API caller awareness**: Prompt asks the model to enumerate sibling code paths that hit the same API surface (so a fix on one ViewModel doesn't ship with an identical buggy sibling), with a grounding warning when the model can't verify them from the diff. Integration-test rule requires assertions to check that request params are both *present and non-empty*, catching empty-string regressions
- **Observability ticket mode**: Logging / alerting / monitoring tickets switch to a QA-runnable test style — Grafana UI inspection, paste-ready LogQL queries against natural traffic, walking every tab of the affected rule, and `[fill in from UI]` placeholders for values the ticket references but doesn't supply. Bans white-box steps QA can't execute (e.g. "deploy the code", "simulate a DB failure")
- **PII protection**: System prompt forbids naming real customers/employees from ticket context as test subjects; a regex pass scrubs any remaining email-shaped strings from the rendered plan as defense-in-depth
- **Boundary & test-layer prompt rules**: Numeric-boundary changes must produce concrete inside/outside example values and matching step text; filtered-collection assertions must check identity, not just cardinality; backend logic coverage is pushed into a dedicated `[Backend]` section instead of inflating UI/voice steps; mobile tickets ban browser-DevTools instructions
- **Derived-field expected values come from spec, not app behavior**: When a test targets a derived value (a computed field, a filtered denominator, an aggregate), the source-of-truth branch decides how strict the assertion is — expected values are derived from the specification, hard-pinned only when the source is confirmed, and flagged for PM with Pass withheld when the source is unconfirmed. On bug tickets this prevents the plan from baking the reported defect in as the pass criterion by matching whatever the app currently renders
- **Copy-only plan shape**: A ticket whose diff is limited to user-visible strings used to get the generator's default shape — a case per route to the same dialog, a case per retired string, a layout case per variant, and a tail of "does saving still work" cases the PR's own component tests already asserted. `src/app/copy_only.py` scans the diff deterministically (a file is copy when it is an i18n/strings file, or when its changed lines differ only *inside* string literals), and when the evidence holds it injects a rule block carrying the extracted new and retired strings. The model does the classifying — the block leads with the disqualifiers and tells an unsure model to ignore it — and when it agrees, the manual checklist is capped at **(distinct copy variants) + 4**: one case per variant, one retired-copy sweep for the whole screen, one layout check at the narrowest width against the longest variant only, one error/empty state, one keyboard/focus pass. Persistence, mutation payloads, cache invalidation and prop wiring are marked `covered_by_unit_test` rather than shipped as manual cases. The model reports its verdict and variant count back in the plan, so the budget check is audited afterwards and shown in the "Grounded in" panel — including which variants one test account cannot reach. Nothing is auto-cut: the rule says to cut from the exclusion list and never from rule 1, and a mechanical trim would drop whichever case sorted last. A PR whose diff GitHub could not return never triggers the block, for the same reason `REQUIRE_SOURCE_GROUNDING` exists. Gated by `COPY_ONLY_RULE_ENABLED` (on by default); single-ticket plans only, because "(variants) + 4" means nothing across a batch that also carries a backend change
- **Concurrent pipeline joins**: Generation is mostly waiting, and two joins that never needed to be sequential no longer are. The three pre-generation legs — Jira attachment downloads, Slack link resolution, and the deliverable classifier's own LLM round-trip — run together instead of adding their latencies; and the regression-grounding critic, which touches only `regression_checklist` while the four case critics touch only the case sections, now overlaps that chain instead of following it. The overlap is in the LLM round-trips: GitHub code searches still serialize behind the process-wide pacing limiter either way, which means either critic can now be the one that runs out of search budget — both report that in `critics_unavailable` rather than reporting a clean check. The tests for both joins are written so that reverting the change deadlocks rather than merely running slower
- **Sticky header quick actions**: Copy / Download / Post-to-Jira are reachable from the sticky test-plan header without scrolling to the end of the test list

## Plan-quality rules

Rules that fire on specific ticket shapes, each one written against a class of
plan that shipped wrong before the rule existed.

- **API-level security negative tests**: acceptance criteria describe the intended user, so a plan derived from them tests the allow path and nothing else — which is how SK-2702's `preview=true` escalation shipped with a passing plan for the very procedure that carried it. `src/app/security_surfaces.py` reads the **diff**, not the AC, and fires when a changed file touches a router or route, auth/middleware, upload or media handling, a share-flow or capability link, a WebSocket/streaming route, or a vendor-backed/paid-AI call. Only the categories the diff justifies are emitted — an upload handler gets MIME/signature, SVG payload, decoded-size and served-header (`Content-Type`, `X-Content-Type-Options`, `Cache-Control`) cases and no rate-limit boilerplate, and a CSS ticket gets no section at all. The client-flag category additionally requires an access-affecting parameter (`preview`, `includeRaw`, `role`, `isAdmin`, …) to actually appear in an added patch line, because a case that cannot name the flag cannot be run. Cases land in their own `security_negative_tests` section, each one **a single principal on a single protocol**: the prompt block explicitly overrides the AVOID REDUNDANCY / PARAMETERIZE rules, because "anonymous, another user, the owner" share their steps and differ in their correct outcome, so a merged case reports one verdict for three independent controls. Every case carries `persona`, `surface: backend_http` and a concrete `request` (method, route, params, auth, protocol) so the UAT runner builds a curl/Postman call and never tries to drive it through the UI or the simulator — no UI can send an absent `Authorization` header or another sender's draft UUID. The grounding rule is unchanged: an expected status code is either read out of the handler with a `file:line` or labelled an assumption. A post-generation audit flags any case that named more than one principal, reporting it in the plan's provenance rather than splitting it, since splitting would mean inventing the second case's expected result
- **Per-role fanout on shared components**: when a ticket touches a shared component and is silent about role (buyer, seller, agent, etc.), a `src/app/shared_component_fanout.py` detector fires and appends guidance that forces a per-role case with an explicit negative-space assertion for fields the role does not consume. Catches the "field renders when data exists" style plan that lets a misplaced role-specific field ship to production
- **Per-test `grounded_in` attribution**: every generated test case carries a `grounded_in` list (e.g. `comments:123`, `PR:456`, `Figma:abc`) rendered as small chips under the test; tests with neither AC coverage nor grounded_in entries get an "Untraced" pill (hidden when the ticket has no ACs at all) so reviewers can spot ungrounded claims at a glance
- **Linked Confluence specs**: Confluence URLs in the Jira description or comments are fetched and injected into the LLM prompt as a LINKED SPECS section so quoted requirements come from the actual spec page, not just the ticket body. Best-effort — per-page failures don't block plan generation
- **Covered-by-unit-tests flag**: cases whose behavior an existing unit test already exercises are flagged and moved into a collapsed section, and excluded from the Jira comment by default

## Web UI

- **Progressive ticket load**: fetching a ticket paints the header, description, labels, status, assignee, story points, and attachments in ~one Jira round-trip via `GET /issue/{key}/basic`, then continues enriching (dev info, comments, PR analysis, parent, children, linked issues, bounce history) in parallel in the background. The partial view stays on screen while enrichment lands so re-fetches don't blank the ticket — the fetch overlay is scoped to the first paint, not the refresh
- **Epic children view**: fetching an Epic lists every child ticket with per-row Generate and Analyze buttons that render results inline beneath the row
- **Plain-language ticket summary**: collapsible section with a lazy-loaded plain-English explanation of what the ticket does. Clicking Summary triggers the fetch **without** expanding the panel — the preview line carries the loading state and eventual snippet, so a second click expands to the full text (or error)
- **Description URL linkification**: `http(s)://` URLs in the Jira description render as new-tab links (trailing `.,;:!?` stays as text so `see https://foo.com.` doesn't point at a 404); long URLs word-break inside the pre so they can't overflow horizontally
- **Story-points chip**: Story-typed tickets render a small `N pts` chip next to the type badge, pulled from Jira's story-points custom field (configurable via `JIRA_STORY_POINTS_FIELD`; defaults to `customfield_10004`) so testers can see effort at a glance without opening the sidebar
- **Inline UX feedback**: auto-scroll to results, per-test checkmarks, a viewport-pinned overall + per-section progress bar, and a hover-only Copy button on each unchecked test card that yanks the title + Preconditions/Steps/Expected/Test data as plain text (flips to a green check for 1.5s to confirm; hidden until hover/focus so it doesn't compete with card content)
- **Shareable URLs**: the active ticket key is mirrored into the URL bar via `?key=…`, so every browser tab is a bookmarkable / refresh-safe handle on a ticket (works alongside the existing per-tab sessionStorage)
- **Live in Jira badge**: Jira posting is update-in-place, so at most one generated version is the one teammates see on the ticket. The run-history drawer tags that version with a pulsing "Live in Jira" chip so users don't double-post or wonder which regeneration is current. The chip is scoped to the latest run — the collapsed banner header always reads as the newest version, so surfacing the chip there when an older version is live read as a second version being live; the chip now only appears on the per-row pill inside the expanded version drawer. A red **Not live in Jira** chip mirrors it (banner header + latest expanded row) when the newest run hasn't been posted yet, so a re-run after prompt changes doesn't quietly leave the stale version live. Posts made from the history banner and from multi-ticket flows now correctly forward `plan_id` so both credit the run in the DB rather than falsely reading as "Not live"
- **Shared per-ticket test progress**: per-test checkmarks are persisted server-side so the whole QA team sees the same checked set; `localStorage` remains an offline fallback. The progress key is derived by the backend (`GET /plans/{id}/progress-key`) so external writers and the UI cannot disagree about it
- **Auto Bug Lens on In Testing**: after the pull-to-testing auto-generate flow lands a test plan for a Bug ticket, Bug Lens is kicked off automatically so the analysis is ready when the tester finishes reading the plan. A nullable `jira_tickets.auto_bug_analysis_dispatched_at` column persists the at-most-once claim so aborted plan generations don't cause a re-fire; manual clicks still work either way. The scroll position is pinned to the test plan when the analysis lands after it, so the view doesn't jump

## Jira browser side rail

A collapsible left rail that lets testers find a ticket without typing a key.
Three drill-down panels mirror Jira's own structure: **Projects → Status
columns → Issues**.

- **Status columns**: the column list is pulled from the project's agile
  board configuration and rendered in board order, so the rail mirrors the
  columns testers already see in Jira instead of exposing every workflow
  status (e.g. hidden "Ready for Release" statuses no longer surface).
  Projects without a board fall back to the full workflow. Within each
  column the statuses are grouped by Jira's `statusCategory` (To Do / In
  Progress / Done), with anything outside the three known categories under
  "Other" so nothing is silently hidden
- **Backlog muting**: for projects that use Jira sprints, issues that aren't on
  the active sprint come back with `in_active_sprint=False` and render with a
  soft visual treatment plus a small "Backlog" tag, so the column makes it
  clear which work is actually in flight. Kanban projects without a Sprint
  field render normally — the backend probes per project before applying the
  filter
- **Issue type badges**: each issue row shows a small color-coded badge
  (Story / Bug / Task / Spike / Epic / Sub-task) using the same palette as
  the main ticket header
- **Pinned + Recent**: pin frequently-used projects with the star icon — they
  appear in a "Pinned" group at the top of the projects list. Recently visited
  projects auto-populate a "Recent" group below it (capped at 5, excluding
  pinned to avoid duplication). Pins and recents are persisted in
  `localStorage` per browser. Both sections are hidden while the filter input
  is in use. When exactly one project is pinned, opening the rail skips
  the project list entirely and drops the tester straight into that
  project's status columns
- **Active-project filter**: the project list defaults to the projects
  Jira has actually seen activity in over the last 30 days (a JQL sweep
  for issues updated in the window returns the distinct project keys),
  so dormant projects are hidden by default. "Show all" is one click away
  and any text filter temporarily disables the active-only cut so a
  search never looks broken. Pinned and recent still surface regardless
- **Ticket row detail**: each issue row shows the assignee's avatar (or a
  muted "Unassigned" placeholder), and right-clicking a row opens a
  native-style context menu with "Open in new tab", "Copy key", and "Open
  in Jira" so the rail doesn't force a left-click hijack of the main
  workspace
- **Empty column state**: an empty status column renders an icon + title
  + contextual body naming the current status and project, instead of
  the earlier "No issues in this column." one-liner that looked like a
  broken row
- **Refresh model**: every panel has a manual ↻ button, and the active panel
  silently re-fetches whenever the tab regains visibility (covers the common
  "I just changed something in the Jira tab" case). A silent 60s interval
  also re-runs the active pane's fetch while the tab is visible, so a Jira
  admin editing a workflow or moving cards on the board shows up in the rail
  without a manual refresh. The three Jira fetches are marked `no-store`
  so a stale HTTP-cached response can't linger. Silent refresh keeps the
  current data on screen while the request is in flight — no spinner flash
- **Selection**: clicking an issue populates the existing input field and
  triggers the normal fetch flow, so the rail is purely additive; the
  paste-a-key input remains the escape hatch for power users
- **Subtask grouping**: when an actual Sub-task issue type and its parent
  both live in the same status column, the subtask row is hidden and its
  parent gets a small `+N sub` pill. Sub-tasks whose parent is in another
  column still appear, indented with a faint vertical tree-line and a
  `SUBTASK OF KEY` caption so the relationship reads at a glance.
  Stories/Tasks under an Epic are *not* affected — they keep their own row
- **Fetch overlay**: while a ticket is loading the rail and main column are
  covered by a centered overlay + spinner so the user gets clear feedback
  instead of a stuck inline button state
- **Auth note**: the rail surfaces only what the configured `JIRA_USERNAME` /
  `JIRA_API_TOKEN` can see. Project list is capped at the first 100 results
  from `/rest/api/3/project/search`

## QA workflow actions

One-click status transitions plus reassignment, to remove the "transition →
pick assignee" two-step from the QA loop. Frontend button visibility is
config-driven via `WORKFLOW_PROJECT_PREFIXES` (default `["SK"]`) — list
additional Jira project keys to surface the QA workflow buttons for those
projects without code changes. The backend endpoint itself still hardcodes
the SK-only check; widening it (e.g. honouring the same setting or a
per-project status map) is the next step before non-SK projects can fully
opt in.

- **Pull to Testing**: shown when the ticket is *not* already in *In Testing*.
  Transitions to *In Testing* and assigns the ticket to the current Jira user
  (the one whose `JIRA_USERNAME` / `JIRA_API_TOKEN` is configured). If the
  ticket has no stored test-plan run and none is loaded in the session, a
  fresh plan is generated automatically — re-pulls and bounce-backs reuse the
  existing plan rather than re-spending on the LLM
- **Pass to UAT**: shown when the ticket is in *In Testing*. Opens an inline
  note form that doubles as the walkthrough authoring surface — a "Steps to
  cover in the video" checklist inside the Notes block pulls the happy path
  from the latest generated plan (capped at 6). Ticked steps get appended
  to the Jira comment as a markdown bullet list; the checklist is silent
  when nothing is ticked so testers who don't engage with it don't clutter
  the comment. The form carries a "Tested in" chip row (Integ / Staging
  / Prod multi-select, preselected by scanning the latest comment +
  description for the corresponding env name), an optional Loom URL textarea
  (one per line, validated against the canonical `loom.com/share/…` shape
  via `models.LOOM_URL_RE` on both the client and the Pydantic request
  model so a typo can't reach Jira as a broken link — each rendered as its
  own paragraph above the fold), an optional attachment dropzone
  (click / drag / paste — PNG / JPEG / GIF / WEBP / PDF / MP3 / TXT / LOG /
  MD / JSON, so API and HTTP work can ship a response body, a curl
  transcript, a log excerpt or a markdown repro as its evidence, and a
  tester who would rather talk through the run than write it up can attach
  a voice note; files upload directly to the
  Jira issue as attachments before the transition runs, and render **inline** in the
  comment via `mediaSingle` ADF nodes with a `📷 <filename>` fallback if the
  media-services UUID can't be resolved. Text and audio payloads have
  nothing to preview, so they always render as a `📎`/`🎧 <filename>`
  callout and are played or read from the Attachments panel). Each uploaded screenshot can be
  **paired to a specific ticked bullet**: a small "attach to step"
  chip on every ticked step lets the tester choose which screenshot lines
  up with which step; the pairing is rendered in the comment as an
  indented `mediaSingle` under that bullet so reviewers see the picture
  right where it belongs. An optional markdown summary is appended
  **inline** to the comment so any URLs stay one-click clickable (no more
  collapsed "Test summary" expand block hiding the share link). A PR-Loom
  discovery panel prefetches `GET /issue/{key}/pr-looms` when the form
  opens so the tester sees exactly which Loom URLs would be harvested
  from merged PR descriptions — or a reason (`no_prs` / `no_merged_prs`
  / `no_looms` / `no_token` / `github_unreachable` / `error`) — before
  they submit; merge state comes from Jira's dev-status API so declined
  PRs and transient GitHub 403/rate-limit errors don't masquerade as
  "nothing merged yet." The same scan also harvests **PR-attached
  screenshots** (GitHub-hosted image URLs in the PR description); ticked
  tiles are downloaded server-side via the GitHub token, uploaded as
  Jira attachments, and inlined in the pass comment the same way
  tester-uploaded screenshots are. Previews route through a new
  `/issue/pr-image-proxy` endpoint so private-repo assets render in the
  browser too, and thumbnails that 404 on load are dropped from the
  panel entirely (the same URL would fail server-side at submit time).
  Submitting transitions to *Ready for UAT*, reassigns to the dev who
  handed it over, and posts a marker-line Jira comment (e.g. `✅ QA
  Passed (Integ + Staging) — ready for UAT`). The endpoint fans its
  work out into three parallel phases with `asyncio.gather` (attachment
  upload / transition lookup / assignee resolution / parent-status read
  → transition + assign → comment + parent auto-transition + subtask
  cascade) so a ticket with a couple of attachments comes back in a few
  seconds instead of the pre-parallel 15–30s that had testers refreshing
  mid-transition. Each phase logs its own timing so a genuinely slow
  ticket points at the offending phase. The ticket's saved walkthrough
  (Loom link, screenshots-as-attachments, notes) is always folded into
  the comment too. **Walkthrough gate**: a high-complexity ticket with
  no walkthrough material (Loom, upload, notes, or PR-attached media)
  is rejected server-side with a 409 `{ error_code:
  "walkthrough_required" }` before any Jira calls fire; the UI opens a
  single override prompt (no two-step client-side nudge) and resubmits
  with `override_missing_walkthrough=true` on confirm. Submitting the
  form empty with no saved walkthrough preserves the original one-click
  pass with no comment. If this is the last sibling sub-task to reach
  Ready for UAT (others already passed or Done), the parent ticket is
  auto-promoted to Ready for UAT in the same call (Epics excluded;
  best-effort, won't fail the primary transition)
- **Fail back**: shown when the ticket is in *In Testing*. Renders as a
  compact **split button** — a "Fail back to <destination>" trigger that
  commits the bounce to the currently selected destination, plus a chevron
  half that opens a small popover to switch between **To Do** and
  **In Progress**. To Do drops the ticket back into the dev backlog; In
  Progress keeps it in-flight for immediate rework. Red is reserved for
  the leading arrow icon and the chevron half while the menu is open, so
  hover no longer floods the toolbar with color. Opens the same inline
  form pattern as Pass to UAT — a *required* Reason field (markdown,
  autofocused, rendered above the fold so devs see *why* without
  expanding), plus an optional multi-Loom textarea and the same
  attachment dropzone (files attached to the issue and rendered
  **inline** in the comment via `mediaSingle` nodes, with the
  `📷 <filename>` / `📎 <filename>` text callout as fallback). Empty submit is rejected
  because a fail-back without a reason has no value. The transition still
  runs even if the comment post fails, matching Pass to UAT. The post-
  action banner is rendered in a warning tone ("Bounced back to …")
  instead of the celebratory green check used for UAT pass, so the
  bounce-back is visually unmistakable
- **Notify chip picker**: Both forms expose an optional Notify row that
  @mentions selected users in the posted comment via a real ADF mention node
  in a trailing `cc:` paragraph (so Jira actually delivers notifications, not
  just text that looks like a tag). Candidates come from people already on
  the ticket: current assignee (starred), prior assignees from the changelog,
  and recent commenters; the configured bot user is filtered out. A
  **debounced typeahead** above the picker hits `/issue/users/search` so a
  PM or manager outside the ticket's own history can be looped in without
  leaving the form — search-added people get merged into the same pill row
  and can be notified or assigned with the existing one-click UX. The
  default assignee still comes from the ticket's own history so a
  search-added person is never silently auto-assigned
- **Also move all subtasks**: Workflow forms include an "Also move all
  subtasks" checkbox (hidden when the ticket has no subtasks) that
  **defaults on** whenever the parent has subtasks — pulling a parent to
  testing almost always means "and pull its subtasks too," so the default
  matches the common case (users can still uncheck). The same default now
  flows through the compact-row Pull button, which had no cascade path at
  all before. When checked, the backend captures the parent's
  *pre-transition* status, then re-applies the target status only to
  subtasks whose current status matches that pre-transition state — so a
  parent moving out of *Ready to Test* only pulls subtasks that were also
  in *Ready to Test*, leaving siblings in unrelated states alone. Subtasks
  whose workflow has no matching transition are skipped silently so a
  partial workflow doesn't break the primary action
- **Assignee fallback chain** (Pass to UAT / Fail back): walks the issue
  changelog for the prior assignee (skipping the bot's own account, since
  Pull to Testing parks the ticket there). If none is found, falls back to
  the top contributor across the ticket's linked PRs (highest
  additions+deletions), mapping GitHub login → Jira account via commit
  author email, then public profile email/name, then login. If neither
  resolves, the ticket is left unassigned and the UI toast says so
- **Assign-to picker on the workflow form**: an "Assign to" pill row is
  exposed on both Pass-to-UAT and Fail-back so the tester can override
  the auto-pick without editing the ticket after the fact. Pass-to-UAT
  opens with the developer (same person the fallback chain would land
  on) preselected so the common "hand it back to them" case stays one
  click; Fail-back opens with nothing selected and unassigns if the
  tester leaves it empty. Candidates come from prior assignees in the
  changelog, and the resolved PR contributor is slotted in behind them
  (surfaced via `GET /issue/{key}/resolved-pr-contributor`) so tickets
  where the tester is the only person in the history still expose the
  developer the server would auto-assign. When the tester sets an
  explicit override the payload carries `assignee_override_set` /
  `_account_id` / `_display_name` and the workflow route honours it
  verbatim, skipping the auto-pick chain (the bot-safety-net still
  redirects to unassigned if the bot user is picked)
- **Available transition guard**: each action looks up the issue's available
  transitions before acting. If the target status isn't reachable from the
  current state the API returns 400 with the list of valid transitions, so
  bad clicks fail loudly instead of silently no-op'ing
- **Endpoint**: `POST /issue/{issue_key}/workflow/{pull-to-testing|pass-to-uat|fail-to-todo|fail-to-in-progress}`

### UAT walkthrough

- **UAT walkthrough**: every plan is tagged with `uat_complexity` and a plain-language "How to test this" summary. The walkthrough is treated as the UAT hand-off *payload* — not a sibling artifact — so authoring lives inside the Pass-to-UAT form itself (a "Steps to cover in the video" collapsible above the Loom input pulls the plan's happy path, capped at 6). Planners can attach a Loom link, drag-and-drop screenshots (uploaded to Jira as attachments and rendered inline in the comment via `mediaSingle` nodes), and setup/repro notes that persist across regenerations; images and videos already uploaded to the linked PR are surfaced in the same form. A single server-side gate (`uat_readiness`) decides whether the ticket needs walkthrough material — high-complexity + no Loom/upload/notes/PR-attached media returns a 409 `walkthrough_required` before any Jira calls fire, and the UI opens a single override prompt instead of the old two-step client-side nudge

## Jira Bug Lens

Analyze bug tickets to go beyond the ticket description and into the code:
- **Bug summary**: Plain-English explanation of what broke and what the user experienced
- **Root cause**: Identifies the exact cause in the code, referencing specific files and logic (requires a linked PR with diffs)
- **Fix explanation**: Describes what the merged PR changed to resolve the bug
- **Fix complexity estimate**: For unfixed bugs, infers the GitHub repo from the ticket and estimates effort required
- **Affected flow & scope of impact**: Identifies which user flows are broken and how wide the blast radius is
- **Test gap analysis**: Highlights what testing was missing that allowed the bug through
- **Regression tests**: Concrete, actionable test cases to prevent the bug from recurring
- **Similar patterns**: Classes of related bugs to proactively look for in the codebase
- **Code evidence**: Deterministic GitHub code search for LLM-suspected symbols — each analysis lists the exact files, line numbers, and code snippets where the suspects appear, with clickable links. Doc files (`.md`/`.rst`) are filtered and zero-hit suspects are hidden.
- **Blame on suspected defect sites**: The LLM is asked for `{path, line}` anchors alongside the symbol names. Each anchor runs through GitHub's GraphQL blame API to attach the commit and PR that introduced the current line, so a change that never got linked to the ticket can still surface as the likely origin, and the analysis renders a "Introduced in" link straight to the culprit commit/PR
- **Multi-ticket support**: Analyze multiple related bug tickets together for a combined root cause analysis
- **Download as .md**: Export the full analysis as a Markdown file
- **Auto-analyzed on Pull to Testing**: When a Bug ticket goes through Pull to Testing and a plan is auto-generated, Bug Lens is dispatched immediately after so the analysis is waiting when the tester finishes the plan. At-most-once per ticket (persisted via `jira_tickets.auto_bug_analysis_dispatched_at`); manual re-runs still work. The action button and busy label change to signal that the run was automatic
- **Collapsed by default when it auto-lands**: the report renders as a keyboard-accessible collapsed card so an auto-run analysis doesn't unfold a long report unprompted underneath the plan. Clicking Download no longer expands the card. The Bug Summary section (which duplicated the ticket description already shown above the card) is gone
- Only shown for `Bug` issue type; automatically uses the same GitHub PR diff pipeline as test plan generation

## Test plan history

Every successful test plan run is persisted to MongoDB so prior versions stay
recoverable and comparable.

- **Prior-runs banner**: When a ticket has prior successful test-plan runs, a
  banner appears above the generate area summarising the latest version and
  expanding to a list of every version with creation time, model, and case count
- **Live in Jira badge**: Posting a plan to Jira updates the existing
  comment in place, so at most one version is the one teammates actually
  see. That version is tagged "Live in Jira" on the run-history rows and
  on the plan banner so reviewers don't double-post or wonder which
  regeneration is current. A red "Not live in Jira" chip mirrors it on
  the banner header and the latest expanded row when the newest run
  hasn't been posted, so a fresh generation doesn't quietly leave the
  stale version live. Posts routed through the history banner and
  multi-ticket flows now correctly forward `plan_id` so those paths
  register in the DB instead of misreading as unposted
- **Side-by-side preview**: Clicking *View* on a row renders the historical
  plan below the live one in muted gray styling, so versions can be read
  side-by-side without losing the active output. The preview is read-only —
  duplicate Post-to-Jira/Copy/Download actions are hidden
- **Version diff**: Clicking *Diff* opens a unified line-diff of the markdown-
  formatted plan against its immediate predecessor (`generated_plans.previous_plan_id`)
- **Auto-chained regenerations**: Single-ticket regenerations automatically set
  `previous_plan_id` and bump `version`, so the chain forms without any user
  action
- The history banner hides while Bug Lens analysis is running or showing, so
  the two flows don't visually overlap; persisting Bug Lens output (with its
  own history banner) is a planned follow-up

## Parent ticket context

*(Phase 6)*

When generating test plans for sub-tasks, the system automatically fetches context from the parent Epic or Story. This is especially valuable because design resources are often attached to parent tickets rather than individual sub-tasks.

### What Gets Fetched from Parent Tickets

- **Parent description**: Business requirements and acceptance criteria
- **Figma designs**: Design specifications linked in parent descriptions
- **Image attachments**: Mockups, screenshots, and design images attached to parent
- **Parent metadata**: Issue type, labels, and summary for broader context

### How It Works

1. System detects if ticket is a sub-task with a parent
2. Fetches full parent ticket data (one additional API call)
3. Extracts Figma URLs from parent description
4. Downloads Figma design context if available
5. Includes parent images (up to 2 from parent, 2 from sub-task = max 4 total)
6. LLM receives both sub-task AND parent context

### Benefits

- **Better context**: Sub-tasks tested with full feature understanding
- **Design access**: Parent-level Figma links and mockups now available
- **Business alignment**: Test plans validate parent-level requirements
- **No extra config**: Works automatically when parent exists

### Example

**Without Parent Context:**
- Sub-task: "Add email validation to form"
- Test plan: Only validates technical implementation

**With Parent Context:**
- Sub-task: "Add email validation to form"
- Parent: "Redesign registration flow" (with Figma designs)
- Test plan: Validates technical implementation AND design requirements from parent Figma

## Linked ticket dependencies

*(Phase 7)*

When generating test plans, the system automatically fetches and analyzes linked tickets to understand dependencies. This provides horizontal dependency context to complement the vertical parent hierarchy.

### What Link Types Are Fetched

The system focuses on high-value link types that directly impact testing:

1. **"Blocks"**: Issues this ticket blocks
   - Downstream work depends on this being correct
   - Test thoroughly to prevent breaking dependent tickets

2. **"Is Blocked By"**: Issues blocking this ticket
   - Prerequisites that must be resolved first
   - Understand API contracts and dependencies

3. **"Causes"**: Issues this ticket may cause
   - Validate fixes don't introduce regressions
   - Test related areas carefully

4. **"Is Caused By"**: Root cause issues
   - Ensure the actual cause is fixed, not just symptoms
   - Particularly valuable for bug tickets

### How It Works

1. System fetches issue links from Jira API
2. Parses link types and directions (inward vs outward)
3. Filters for relevant link types (blocks, causes)
4. Fetches basic details for each linked issue (max 5 per type)
5. LLM receives linked context with clear relationship labels

### Benefits

- **Dependency awareness**: Know what must be done first
- **Impact analysis**: Understand what depends on this work
- **Better prioritization**: Test critical paths more thoroughly
- **Root cause validation**: Ensure bugs are truly fixed
- **Regression prevention**: Validate fixes don't break related tickets

### Example

**Scenario:**
- PROJ-101: "Implement Stripe integration" (Status: Done)
- PROJ-102: "Add payment UI" (blocked by PROJ-101)

**When testing PROJ-102:**
- System detects "blocked by PROJ-101"
- Fetches PROJ-101 details (API endpoints, data models)
- LLM generates test plan that validates integration with PROJ-101's API
- Test plan includes prerequisites: "Verify PROJ-101 Stripe API is available"

**Another Scenario:**
- Bug PROJ-200: "Login fails on mobile"
- Root cause: PROJ-150 "Session timeout too short"

**When testing the fix:**
- System detects "caused by PROJ-150"
- Fetches root cause context
- Test plan validates both symptom AND root cause are fixed
- Includes test: "Verify session timeout increased (root cause from PROJ-150)"
