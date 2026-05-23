## 5 wow ideas to improve jira-testplan-bot

**1. Plan → runnable test code generation.** Close the loop: take the generated test plan and emit executable tests (Playwright / Cypress / pytest) that mirror the repo's existing patterns. The GitHub integration in [src/app/llm_client.py](src/app/llm_client.py) already pulls test files for context — extend it to *write* tests, not just plan them. Biggest impact because it removes the manual translation step QA does every day.

**2. Living test plan with diff mode.** _[PARTIAL — diff UI in [PlanDiffModal.jsx](frontend/src/components/PlanDiffModal.jsx) + `previous_plan_id` field exist; no auto-regen on ticket update]_ When a ticket gets a new commit, comment, or linked issue, auto-regenerate and show a *diff* against the last version: "3 new test cases, 1 now obsolete." Solves the "no test plan history" gap the tool has today and stops plans from rotting silently mid-sprint.

**3. Feedback-trained few-shot loop.** _[PARTIAL — `FeedbackEvent` model in [feedback.py](src/app/db/models/feedback.py) + repository exist; no API wiring, no few-shot injection in LLM prompts]_ Add thumbs up/down on each generated test case, persist outcomes (which caught real bugs, which were noise), and feed the best examples back into the prompt as few-shot exemplars per project. Turns the tool from stateless into something that measurably gets smarter for *your* team — a great story for a review.

**4. Blast-radius / regression graph in Bug Lens.** Beyond root-cause text, build an import/call graph from the PR diff and render a visual of which other flows touch the changed code, ranked by regression likelihood. Bug Lens in [src/app/bug_lens_routes.py](src/app/bug_lens_routes.py) already has the PR diff — the graph is the wow layer.

**5. Flaky-test detective + auto-triage.** Poll GitHub Actions history, detect flakes, and auto-file a Jira ticket with Bug Lens-style analysis attached (first-flake commit, likely culprit, suggested fix). Makes the tool a proactive agent, not just a request/response box.

**6. Bounce-risk predictor + auto-hardened plan.** `bounce_history` already records when a single ticket got kicked back from UAT/QA to To Do — but the real signal is *across* tickets. For every new generation, find the K-nearest prior bounced tickets (overlap of touched files from dev_info, shared parent epic, similar reporter/component), extract bounce *reasons* from those transition comments, compute a bounce-probability score for the current ticket, and auto-inject regression cases that target each recurring failure mode with a "we missed this in JIRA-1234" provenance line. Wow factor: unlike flaky-test detectors (idea 5) or generic risk dashboards, this uses a signal almost no other QA tool has — your team's own Jira bounce-back history — and turns the bot from a passive *describer* of work into an active *defender* against repeat failures. Schema cost is tiny (bounce_history already lives on `jira_tickets`); the prompt gets one new "Past failure patterns" block and the UI gets a risk badge with click-through to the source bounces.

---

## 5 wow tools to build for promotion (QA → SDET / senior)

**1. Coverage-aware test prioritization engine.** For any PR, rank existing tests by probability of catching a regression using file-level coverage + historical failure correlation. Plug into CI to run the top-N first. Shows graph/static-analysis chops and saves real CI minutes — easy to quantify for a promo packet.

**2. Production incident → test-gap analyzer.** Ingest Sentry/Datadog/Rollbar alerts, ask "did our test suite exercise this failure mode?", and auto-generate the missing tests + Jira ticket. Positions you as the bridge between SRE and QA, which is exactly the scope level promotion committees look for.

**3. Feature-flag combinatorial risk dashboard.** Pull all LaunchDarkly/GrowthBook flags, cross-reference with test history, and surface flag *combinations* that have never been exercised together. Release-safety win that's visible to eng leadership.

**4. Org-wide test health score.** A dashboard that ingests CI data across repos and scores each team on flakiness, coverage trend, test duration, MTTR. Makes QA a data-driven function leaders can act on — and puts your name on the artifact everyone checks.

**5. Synthetic journeys from real telemetry.** Feed Amplitude/Heap/GA data into a generator that emits E2E tests for the top real user paths ("40% of users do X→Y→Z — here's the suite"). Ties product analytics to testing, a classic senior-level move that shows you think about users, not just tickets.

---

## 5 ways to leverage the new Neon Postgres data

Now that runs, generated plans, individual test cases, ticket snapshots, and feedback events are persisted, here's what to build on top — ordered by leverage.

**1. Wire feedback → few-shot retrieval (plans that learn per project).** _[PARTIAL — schema built in [feedback.py](src/app/db/models/feedback.py); no retrieval query, no prompt injection]_ The `feedback_events` table is built but unwired. Add thumbs-up/down to the plan and per-case UI, then on each new generation retrieve the top-N upvoted plans from the same `project_key` + `issue_type` and inject them as in-context examples. Every vote becomes a quality lever and gives per-project specialization without prompt edits.

~~**2. "We've analyzed this ticket before" surfacing.** `runs.ticket_keys` already indexes prior runs per ticket. When a user opens a ticket with previous runs, show a banner: "Last test plan generated 2026-04-12 — view / diff / regenerate." For regenerations, `generated_plans.previous_plan_id` is in place — render a side-by-side diff so reviewers see what changed instead of re-reading the full plan.~~ ✅ _DONE — [RunHistoryBanner.jsx](frontend/src/components/RunHistoryBanner.jsx) + `/runs/by-ticket/{ticket_key}` endpoint in [runs_routes.py](src/app/runs_routes.py), diff via `previous_plan_id`._

**3. Persist Bug Lens outputs and feed them into test plan generation.** _[PARTIAL — persisted in [bug_analysis.py](src/app/db/models/bug_analysis.py) (root_cause, regression_tests, affected_flow); not fed back into test plan prompts]_ Bug Lens currently writes to `runs` + `jira_tickets` but not the analysis itself — a big missed asset. Persist root cause / suspected files / proposed regression tests, then when generating a test plan for a *child* ticket or one touching the same files, pull the prior Bug Lens "regression tests" section as seed cases under the `regression` category.

**4. Repeat-bug / similar-ticket clustering.** With Bug Lens analyses persisted plus `parent_key` and PR file paths, flag "this looks like JIRA-1234 from 6 weeks ago" by overlap on suspected files + root-cause keywords. High-value for QA: surfaces flaky areas and lets the tool auto-suggest the previously effective regression tests.

**5. Cost & quality observability.** _[PARTIAL — per-run metrics captured in `runs` (cost_usd, tokens, latency_ms, status, error_code); no `/admin` dashboard]_ `model`, `prompt_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `status`, and `error_code` are already captured per run. A small `/admin` page giving cost per project, failure rate by run type, and (once feedback is live) a model A/B chart — upvote rate vs. cost — directly informs model selection. Mining `error_code` clusters also exposes specific prompt failures that are easy wins.
