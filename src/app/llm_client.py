"""
LLM client abstraction layer.

Supports multiple LLM providers with a unified interface:
- Ollama (local, free)
- Claude API (Anthropic, paid)

Switch providers by changing LLM_PROVIDER in .env
"""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import is_dataclass
from typing import Any

import httpx

from .config import settings
from .models import BugAnalysis, TestPlan

_VALID_FIX_STATUSES = ("not_fixed", "in_testing", "fixed")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PII_PLACEHOLDER = "<test-account>"

_TICKET_KEY_NUM_RE = re.compile(r"(\d+)\s*$")


def _ticket_key_recency(ticket_key: str) -> int:
    """Numeric suffix of a Jira key, used to rank tickets by recency.

    Higher number = newer (SK-2194 > SK-2138). Tickets whose key has no
    trailing number sort last (treated as oldest) so they don't accidentally
    win a conflict on bad input.
    """
    match = _TICKET_KEY_NUM_RE.search(ticket_key or "")
    return int(match.group(1)) if match else -1


def _sort_tickets_newest_first(tickets: list[dict]) -> list[dict]:
    """Return tickets ordered newest-first by numeric suffix of ticket_key."""
    return sorted(
        tickets,
        key=lambda t: _ticket_key_recency(t.get("ticket_key", "")),
        reverse=True,
    )


def _scrub_emails(text: Any) -> Any:
    """Replace any email-shaped substring with a generic test-account placeholder.

    Defense-in-depth against real customer/employee emails from Jira context
    leaking into rendered test steps despite the system-prompt guardrail.
    """
    if not isinstance(text, str):
        return text
    return _EMAIL_RE.sub(_PII_PLACEHOLDER, text)


def _scrub_test_case(case: Any) -> Any:
    """Recursively scrub email-shaped PII from a test-case dict or list."""
    if isinstance(case, dict):
        return {k: _scrub_test_case(v) for k, v in case.items()}
    if isinstance(case, list):
        return [_scrub_test_case(item) for item in case]
    return _scrub_emails(case)


def _scrub_test_plan_data(test_plan_data: dict) -> dict:
    """Strip email addresses from any field in the LLM-returned test plan."""
    return {key: _scrub_test_case(value) for key, value in test_plan_data.items()}


def _normalize_fix_status(raw: Any, legacy_is_fixed: Any = None) -> str:
    """Coerce LLM output to a valid fix_status. Falls back to legacy is_fixed boolean."""
    if isinstance(raw, str) and raw in _VALID_FIX_STATUSES:
        return raw
    if isinstance(legacy_is_fixed, bool):
        return "fixed" if legacy_is_fixed else "not_fixed"
    return "not_fixed"


def _safe_get(obj: dict | Any, key: str, default: Any = None) -> Any:
    """
    Safely get a value from either a dict or a dataclass instance.

    Args:
        obj: Either a dictionary or a dataclass instance
        key: The key/attribute name to access
        default: Default value if key doesn't exist

    Returns:
        The value from the object, or default if not found
    """
    if isinstance(obj, dict):
        return obj.get(key, default)
    elif is_dataclass(obj):
        return getattr(obj, key, default)
    return default


_VOICE_KEYWORDS_RE = re.compile(
    r"\b("
    r"voice[- ]?over|voiceover|"
    r"screen[- ]?reader|screenreader|"
    r"text[- ]?to[- ]?speech|speech[- ]?to[- ]?text|"
    r"speech recognition|"
    r"talkback|nvda|jaws|"
    r"dictat\w*|"
    r"tts|stt|"
    r"voice"
    r")\b",
    re.IGNORECASE,
)


def _is_voice_ticket(summary: str | None, description: str | None) -> bool:
    """Return True if the ticket involves voice I/O or screen-reader behavior.

    Word-boundary matching avoids false positives like 'invoice' or 'choice'.
    """
    combined = f"{summary or ''}\n{description or ''}"
    return bool(_VOICE_KEYWORDS_RE.search(combined))


_OBSERVABILITY_KEYWORDS_RE = re.compile(
    r"\b("
    r"alerting|alert rule|alert rules|alertmanager|"
    r"grafana|loki|logql|promql|prometheus|"
    r"datadog|new relic|newrelic|sentry|honeycomb|splunk|elastic|kibana|opensearch|"
    r"observability|telemetry|"
    r"structured log\w*|log line\w*|log event\w*|log spike|"
    r"error rate\w*|failure rate\w*|"
    r"pagerduty|opsgenie|on-call|oncall"
    r")\b",
    re.IGNORECASE,
)


def _is_observability_ticket(summary: str | None, description: str | None) -> bool:
    """Return True if the ticket is about logging, alerting, or monitoring.

    These tickets add structured log events, alert rules, dashboards, or
    notification routing — most of what's "added" is white-box and a manual
    QA cannot exercise it by breaking the database or mocking an SDK. The
    detector triggers the OBSERVABILITY_TESTING_GUIDANCE block, which
    reframes tests around what QA can actually verify (Grafana UI config
    inspection, LogQL queries against natural traffic, one synthetic
    end-to-end alert with dev pairing).

    Word-boundary matching keeps this from firing on incidental mentions
    like 'alert the user' or 'log in'.
    """
    combined = f"{summary or ''}\n{description or ''}"
    return bool(_OBSERVABILITY_KEYWORDS_RE.search(combined))


UI_GROUNDING_GUIDANCE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔎 UI GROUNDING — DO NOT INVENT UI ELEMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Acceptance criteria are written from the *intended* user experience, not the
shipped one. Engineering scope often shifts: an AC says "tap the Edit button"
but the PR replaces the button with a pencil icon, or drops it entirely. If
you take AC wording at face value, you will write tests for controls that
don't exist.

For every UI element you name in a test step — buttons, links, modals,
popovers, fields, tabs, toasts — you must be able to point to it in ONE of
these sources:

1. **The PR diffs / "Key Code Changes" section above** — the literal element
   appears in added/modified code (component label, JSX text, testID,
   string constant, route, icon name).
2. **The testID reference** — the element appears in the testID list (when
   one is provided for this app).
3. **The ticket description or attached screenshots** — the element is
   shown or described concretely.

When you CANNOT find the element in any of those sources:

- Do NOT invent the literal label. Either describe the action generically
  ("trigger the bulk-fill edit flow") OR use the wording from the AC and
  flag the test in `grounding_warnings`.
- Add one entry to `grounding_warnings`:
  `{ac_id: "<AC ID>", missing_element: "<element you couldn't verify>",
    explanation: "<one sentence: where you looked and what you didn't find>"}`.
- Keep the test in the plan — a reviewer will confirm whether the AC's
  UI claim was actually implemented. Do not drop coverage just because you
  couldn't ground a label.

This rule is about elements you SAY THE USER INTERACTS WITH. Outcome text
(toast messages, error copy) only needs grounding when you quote it verbatim
in `expected_result`. If you paraphrase the outcome, no warning is needed.
"""


OBSERVABILITY_TESTING_GUIDANCE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 OBSERVABILITY / ALERTING / LOGGING — SPECIALIZED TEST GUIDANCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This ticket adds structured log events, alert rules, dashboards, or notification routing. Most of the "feature" is white-box behavior that a manual QA CANNOT exercise. Reframe the test plan around what QA can actually do.

━━ WHAT QA CAN ACTUALLY DO (PREFER THESE) ━━
1. **Inspect alert-rule configuration in the Grafana UI.** Walk QA to the exact rule: which folder, which rule name (or UID if the ticket gives one), and what to verify on the rule's Edit page — query string, datasource, evaluation interval, `for` duration, threshold, severity label, contact point / notification channel. This is high-value and 100% testable.
2. **Run a copy-pasteable LogQL/PromQL query against real staging traffic.** After deploy, normal traffic produces some natural failures. Have QA open Grafana → Explore → paste the full query string (including label selectors and time range) and verify the event appears with the expected JSON shape and required fields.
3. **One end-to-end synthetic alert firing test, dev-assisted.** Tag it `[Dev-assisted]` in the title. ONE test, not eight — a developer crosses the threshold (e.g. by replaying failing requests against staging), and QA confirms the rule transitions to Firing and the Slack/PagerDuty notification arrives in the named channel with the expected content. Don't fragment this per event type.
4. **Verify notification routing.** Open the alert rule → Notifications tab (or notification policy tree) → confirm the contact point matches the channel the ticket names (e.g. `#skunks-prod-alerts`, Slack receiver name).
5. **Dashboard panel checks** — only if the ticket changes a dashboard. Walk QA to the specific dashboard URL/UID and the panel name, and confirm the panel renders without errors against the new event data.

━━ BANNED STEPS (DROP THESE — THEY ARE NOT MANUAL QA) ━━
- ❌ "Deploy the updated API code to staging environment" — that is a developer/CI step, not a test step. Move it to `preconditions` once, not into every test.
- ❌ "Intentionally break a generation path" / "trigger Azure to reject" / "trigger an Azure RAI rejection" — unless the ticket SUPPLIES a known-triggering prompt verbatim, QA has no concrete way to do this. Drop the test or convert it to a smoke test (`[Smoke]`) that waits for natural traffic to produce the event.
- ❌ "Simulate a database write failure" / "use a mock that throws" / "temporarily block DB access" — QA does not have infrastructure or code-mock access. These are unit-test concerns; drop them.
- ❌ Verifying internal log fields that have no Loki-observable consequence (e.g. "verify `level: warn` is used not `error`", "verify `command` field uses class name not method name", "verify the route-level catch detects the retries-exhausted sentinel"). If the field IS observable in Loki, write the query that reads it; otherwise drop the test — it is a unit test, not manual QA.
- ❌ "Count the number of `generation_failed` events to verify no duplicate" — drop unless the LogQL query and time bounds are concrete. Even then, this is fragile and better left to unit tests.
- ❌ "Verify sensitive data is redacted" without a concrete trigger — this needs an actual triggering input that would have produced sensitive content. Without one, the test is unrunnable.

━━ MANDATORY: GIVE QA THE EXACT QUERY ━━
Every Loki/Prometheus test MUST include the FULL query string in `test_data`, not a fragment. The query must be paste-ready into Grafana Explore.
- ❌ BAD `test_data`: "Filter for |= 'generation_failed'"
- ✅ GOOD `test_data`: "Datasource: grafanacloud-logs. Time range: Last 1 hour. Query: `{app=\\"agent-coach-api\\"} |= \\"generation_failed\\" | json | line_format \\"{{.command}} | {{.errorKind}} | {{.errorMessage}}\\"`"

Same for alert-rule inspection: name the folder AND the rule (or UID), don't just say "navigate to the alert rule".
- ❌ BAD: "Navigate to the alert rule and verify the datasource"
- ✅ GOOD: "Grafana → Alerting → Alert rules → folder `Ayce` (uid `a78c11ec-5908-4c73-8147-a6518b61d23b`) → open `Generation Failure Spike` → click Edit → confirm Datasource = `grafanacloud-logs`, Query contains `{app=\\"agent-coach-api\\"} |= \\"generation_failed\\"`, Evaluation interval = 1m, For = 5m, Threshold > 5, Contact point = `Slack - skunks-prod-alerts`"

━━ OBSERVABILITY TEST-PLAN ORGANIZATION (MANDATORY) ━━
Prefix every test case title (in `happy_path`, `edge_cases`, and `integration_tests`) with ONE of these group tags, EXACTLY as shown:

  `[Alert config]`   inspecting a Grafana alert rule in the UI: query, threshold, datasource, contact point
  `[Log shape]`      running a LogQL/PromQL query and verifying the JSON shape / required fields of returned events
  `[Smoke]`          post-deploy verification using natural staging traffic — no synthetic trigger
  `[End-to-end]`     synthetic alert firing through to the notification channel (almost always `[Dev-assisted]`)
  `[Notification]`   contact-point / receiver / channel routing checks
  `[Dashboard]`      Grafana dashboard panel checks (only if the ticket changes a dashboard)

If a test genuinely requires developer help (replaying requests, crossing a threshold deliberately), ALSO append `[Dev-assisted]` to the title. Cap `[Dev-assisted]` tests at 1–2 across the whole plan — fragmenting synthetic tests adds no coverage.

━━ CONSOLIDATE AGGRESSIVELY ━━
- Don't write one happy-path test per structured-log event. If the ticket adds five events (`generation_failed`, `generation_retries_exhausted`, `streamed_text_write_failed`, …), ONE `[Log shape]` test with a table of `{event_name → required fields → triggering condition}` in `test_data` is better than five near-identical tests.
- Don't write one alert test per command name. Verify the rule once; the `by (command)` grouping is the rule's responsibility.

━━ PRECONDITIONS (USE THEM) ━━
Almost every test in this ticket type shares the same setup: "API deployed to staging; Grafana access to the org with the `grafanacloud-logs` datasource; Slack access to the alerts channel." Put that in `preconditions` ONCE — do NOT repeat "Deploy the updated API code" as step 1 of every test.

━━ VERIFY THE WHOLE RULE WHEN EDITING AN EXISTING ALERT ━━
When a test edits an existing alert rule (changing a datasource, threshold, query, or contact point), do NOT scope the test to only the field that changed. While QA is in the Edit page, have them walk every tab and confirm adjacent fields look right too — a one-line config change can silently break a `for` duration, an annotation template, or a notification routing rule.

For ANY `[Alert config]` test that targets an existing rule, the steps MUST cover:
- **Query tab** — datasource, full LogQL/PromQL expression, time window, grouping
- **Conditions / Threshold** — operator, value, reducer, evaluator
- **Evaluation** — evaluation interval and `for` duration
- **Annotations** — summary and description templates render as expected
- **Labels** — severity, team, any routing labels
- **Notifications tab** — contact point AND notification policy match what the ticket specifies
- **State at rest** — current rule state is `Normal` and health is `OK` (NOT `NoData`, `Error`, or unexpected `Firing`)

This catches accidental regressions on fields adjacent to the change. It is cheap — QA is already on the Edit page.

━━ MARK UNKNOWNS WITH [fill in] / [capture from UI] — DO NOT FABRICATE ━━
If the ticket REFERENCES a value but does not pin it down — for example, the ticket says "the rule should route to the team Slack channel" without naming the channel, or it lists a rule UID but not the exact LogQL expression — DO NOT invent a value. Instead, write the step using one of these placeholders:

  `[fill in from UI]`               — the tester captures the current value on screen and verifies it manually
  `[capture from current config]`   — same idea, value is the source of truth in Grafana
  `[paste expected value here]`     — the team should supply this; the test plan author didn't know it
  `[confirm against actual receiver]` — the ticket named a value, but the test plan author is not certain it's correct

Examples:
- ❌ BAD: "Verify Contact point = `Slack - skunks-prod-alerts`" (when the ticket only said "team Slack channel" — the name might be wrong)
- ✅ GOOD: "Verify Contact point matches the team's alerts channel [confirm against actual receiver — original ticket referenced `Slack - skunks-prod-alerts`]"
- ❌ BAD: "Verify the Query is `count_over_time({app=\\"agent-coach-api\\"} |= \\"chat_rate_limit_exceeded\\" [24h])`" (fabricated expression — ticket didn't supply it)
- ✅ GOOD: "Verify the Query targets the agent-coach-api app and counts chat-rate-limit events over a 24h window. [paste exact expression from current rule — capture and confirm]"

This pattern is HONEST: it tells QA *what* to verify and admits *what the LLM didn't know*. A fabricated assertion that turns out to be wrong wastes the tester's time AND makes them mark a real-bug as a false positive.
"""


VOICE_TESTING_GUIDANCE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎙️ VOICE / SCREEN READER — SPECIALIZED TEST GUIDANCE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This ticket touches voice input, voice output, or screen-reader behavior. SKIP obvious cases the tester already knows to check (mic permission granted, labels exist, TTS produces sound). Focus on the non-obvious failure modes below, and ONLY include cases plausibly affected by what the ticket / diff actually changes. If the ticket is purely TTS, skip STT-specific cases, and vice versa.

**REQUIRED when the ticket involves streaming, concurrent audio, or both voice input and voice output in the same flow** — these two are frequently missed, include them unless the diff clearly rules them out:
- **Barge-in** — user speaks while the app is still talking. Does voice input get gated, does voice output cancel, or does the mic record the app's own voice output and transcribe it back as input? → `edge_cases` / `error_handling`.
- **Voice-output interruption policy** — a second voice-output trigger fires mid-utterance (rapid message, user re-asks, tool result arrives). Is it queued, cancel-and-replace, or overlapping into garbled audio? → `edge_cases` / `boundary`.

**Voice input (speech-to-text / dictation) — creative scenarios:**
- Mid-capture audio route change — Bluetooth headset connects/disconnects while dictating; does capture continue, restart, or silently drop?
- Domain vocabulary fidelity — proper nouns this feature actually uses (street names, MLS IDs, brokerage names, unit numbers like "3B" / "Apt 12-A") transcribe without autocorrect mangling.
- Numeric ambiguity — "three point five" vs "three fifty" vs "thirty-five hundred" vs "$3,500"; normalization must match the field type (currency vs year vs count vs decimal).
- Long silence mid-utterance — does auto-stop cut the user off while they're still thinking?
- Partial iOS permission — speech recognition granted but microphone denied (or vice versa); specific failure mode that bypasses the combined "permissions OK" path.
- Locale mismatch — device set to en-GB / es-MX while app forces en-US; which wins, and does recognition quality drop?

**Voice output (text-to-speech) — creative scenarios:**
- Pronunciation of the exact strings THIS feature emits — currency, HOA/MLS abbreviations, street addresses, ordinals, decimals, negative values.
- Audio focus — another app playing music or a call active; confirm ducking / pause / interrupt behavior is the intended choice.
- Rapid re-triggers — user taps the TTS control 5× quickly; queue bloat vs debounce vs cancel-and-restart.
- Silent mode / DND — is TTS still audible, and is that the intended behavior for this feature?

**Screen reader (VoiceOver / TalkBack / NVDA / JAWS) — creative scenarios:**
- Live-region + focus-change race — content updates while the reader is mid-sentence on the previous element; is the update lost or does it interrupt?
- Dynamic content mid-scan — list re-renders while the reader is scanning; does position survive?
- Custom gesture vs passthrough conflict — app-level swipe overlaps VoiceOver's passthrough gesture; which wins?
- Focus restoration after modal dismiss mid-announcement — lands on the correct trigger element?
- Text-expansion in localized strings (German, Finnish often 2–3× longer) — does visual truncation hide content from sighted users while the reader still reads the full string?
- iOS VoiceOver vs Android TalkBack parity — walk the same flow on each; flag any divergence.

━━ VOICE TEST-PLAN ORGANIZATION (MANDATORY) ━━
Voice tickets produce many tests. Keep the output scannable — PREFIX every test case title (in `happy_path`, `edge_cases`, and `integration_tests`) with ONE of these group tags:

  `[Prompts]`       agent dialogue quality: intro, clarification, confirmation, follow-ups
  `[Voice input]`   speech capture, transcription, mic handling, recognition quality
  `[Voice output]`  playback, pronunciation, audio focus, interruption policy
  `[Streaming]`     concurrent generation + playback, SSE / WebSocket transport, timing, barge-in
  `[UI]`            on-screen voice components (address cards, transcripts, settings sheet)
  `[Security]`      prompt injection, user-context sanitization, payload / rate caps
  `[Screen reader]` VoiceOver / TalkBack / NVDA / JAWS (only if the ticket touches a11y)

Use the tag text EXACTLY as shown above (including the spaces inside `[Voice input]` and `[Voice output]`). Do NOT abbreviate to `[STT]` or `[TTS]` — testers shouldn't have to decode acronyms.

Example title: `[Voice output] Agent reads captured currency amounts in natural language`.

**Ordering within each section:** first by priority (critical → high → medium) as already required, THEN keep same-group tests adjacent so the reader sees all `[Prompts]` together, all `[Voice input]` together, etc.

**Consolidate aggressively:** if multiple tests differ only by provider/model (e.g. xai vs whisper-1 vs gpt-4o-transcribe), collapse to ONE test with a provider/model table in `test_data`. Do NOT emit one test per provider. Same rule for different speech-to-text engines, text-to-speech voices, or LLM models.
"""


SYSTEM_PROMPT = """You are an expert QA engineer with 10+ years of experience creating comprehensive test plans. Your role is to generate thorough, actionable test cases that catch bugs before they reach production.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ CRITICAL: STAY GROUNDED IN ACTUAL REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**YOU MUST ONLY TEST WHAT IS EXPLICITLY MENTIONED:**
- ONLY create test cases for features/fields/UI elements explicitly described in the ticket, PR changes, or context
- DO NOT invent test cases for features that "should" exist based on your domain knowledge
- DO NOT test for standard features unless they are specifically mentioned or modified
- If the ticket says "add a button", only test that button - don't test the entire page layout unless mentioned

**BEFORE ADDING EACH TEST CASE, ASK:**
1. "Is this feature explicitly mentioned in the ticket/PR/context?"
2. "Am I making assumptions based on what similar applications typically have?"
3. "Would someone reading the ticket description expect this test?"
4. "If this test is scoped to a specific context (Buyer, Seller, etc.), does the ticket text or diff explicitly confirm this change applies to that context — or am I inferring it from a section heading?"

If you answer "no" or "not sure" to question 1 or 4, DO NOT include that test case.

**EXAMPLES OF WHAT NOT TO DO:**
❌ Ticket: "Fix login button styling" → Don't add tests for password reset, OAuth, or session management
❌ Ticket: "Generate PDF report" → Don't add tests for watermarks, headers, footers unless mentioned
❌ Ticket: "Add export feature" → Don't test for file formats not mentioned in the ticket

**DO NOT INVENT UI STATES OR OPTION VALUES:**
- NEVER assume a dropdown/selector has an "undefined", "empty", or "null" state unless the ticket explicitly says so
- NEVER test "leaving a field unselected" unless the ticket or context confirms the field can actually be empty (e.g. it has a placeholder like "Select an option" or the ticket mentions missing input handling)
- NEVER invent option values (e.g. "Buyer", "Seller", "Split") for a selector unless those exact options are listed in the ticket, PR diff, or testID reference
- If the ticket mentions a field/selector, only use the specific values explicitly named in the ticket description, acceptance criteria, or test data provided
❌ Ticket: "Handle Transfer Tax payor selection" → Don't test "undefined" payor unless the ticket explicitly describes that state

**DO NOT INVENT FORM FIELDS:**
- NEVER include a form field in test steps unless it is: (a) explicitly named in the ticket description, (b) visible in the testID reference or screen guide, or (c) confirmed in the PR diff
- Domain knowledge about what fields "should" exist in a real-world form is NOT a valid reason to include a field
- If you are not certain a field exists in this specific app, omit it
❌ The app is a real-estate calculator → Don't add "City Transfer Tax" or other domain-typical fields unless they are listed in the testID reference or ticket description

**DO NOT INVENT UI COPY, STRINGS, OR MESSAGES:**
A test step that quotes UI text is asserting that *exact* string exists in the app. NEVER fabricate that string from domain plausibility — if the source isn't in your context, you cannot quote it.

- A quoted string (in `'single'`, `"double"`, or backtick quotes) is allowed in a step or expected result ONLY when the EXACT same string appears in: (a) the ticket description / acceptance criteria, (b) the PR diff / code patches, or (c) the testID reference / screen guide
- This applies to ALL UI copy: share/email message bodies, modal text, toast/alert content, button labels, placeholder text, success/error messages, email subjects, push-notification copy, default form values
- Placeholders like `[Client FName]`, `{firstName}`, `<NAME>` are also UI copy — do NOT invent placeholder syntax for an app
- If the ticket asks you to verify a piece of copy but does NOT supply the exact text, write the step in terms of *intent*, not verbatim content. Use phrases like "contains the client's first name and references closing costs" instead of quoting a fabricated body
- When in doubt, omit the quote. A vague-but-true step is better than a precise-but-fabricated one — testers will mark a fabricated quote as a bug that doesn't exist

❌ BAD (fabricated): `Verify pre-populated message contains: 'Hi [Client FName], here is a report with detailed closing costs...'`
✅ GOOD (intent-based): `Verify the share sheet message body is pre-populated and references the client and the closing-cost report`
✅ GOOD (grounded): If the ticket or diff contains the literal `DEFAULT_SHARE_MESSAGE = "Your personalized Net Sheet is ready!..."`, you MAY quote that exact string

**DO NOT INVENT TEST DATA VALUES FROM UNIT TESTS OR FIXTURES:**
A unit test's fixture values (e.g. `finhoadues1: '275'`, mock API responses, seeded DB rows) are *synthetic inputs the developer chose to exercise a code path* — they are NOT values the tester will see when running the test plan against a real environment. NEVER copy a fixture value into a test step or expected result as if it were a known, observable quantity.

- Asserting "Verify HOA Dues = $275" only works if the tester's chosen address actually has $275 dues. The test plan does NOT supply that address — so the value is unknowable in advance.
- This applies to ALL fixture-derived values: dollar amounts, dates, account IDs, addresses, lat/lng, response payloads, count totals, percentage rates, default seeds — anything that lives in test fixtures, mocks, or seeded DBs but is NOT a UI default or constant defined in app code.
- Internal field, column, or schema names from fixtures/code (e.g. `finhoadues1`, `fin_hoa_period_1='Monthly'`, `fin_hoa_yn_std='N'`) are equally fixture-derived and equally non-actionable for a manual tester. A tester cannot query the upstream data source by column name — they can only pick properties by user-observable characteristics. Describe the *kind* of property required, not the database fields it must satisfy.
- Write expected results in terms of the *relationship* between input and output, not a specific number: "Verify the HOA Dues field auto-populates with the same monthly value shown in the property data modal" instead of "Verify HOA Dues = $275".
- If the test requires a specific value, instruct the tester to capture it from the prior step: "Note the HOA dues value displayed in the modal" → later → "Verify the HOA Dues field matches the value noted in step N".
- A constant defined in app code (e.g. `DEFAULT_TAX_RATE = 6.75`) IS groundable and MAY be quoted. A value that only appears in `*.test.ts` / `*.spec.ts` / fixture JSON / mock response files is NOT.

❌ BAD (fixture leak): `Enter a property with finhoadues1: '275', finhoaperiod1: 'Monthly'` ... `Verify HOA Dues field shows $275`
❌ BAD (schema leak): `Enter a property where Spoke VRE has fin_hoa_yn_std='N'` or `Enter a property with fin_hoa_period_1='Annually'`
✅ GOOD (intent-based): `Enter a property address that has monthly HOA dues in Spoke VRE` ... `Verify the HOA Dues field is auto-populated with the monthly dues amount shown in the property data modal`
✅ GOOD (user-observable): `Enter a property address known to have no HOA` instead of `fin_hoa_yn_std='N'`; `a property with annual HOA dues` instead of `fin_hoa_period_1='Annually'`
✅ GOOD (capture-then-compare): `Note the 'Estimated Monthly HOA Dues' value shown in the modal` ... `Verify the HOA Dues field auto-populates with the value noted above, in monthly mode`

**SKIP OPTIONAL FIELDS WITH ACCEPTABLE DEFAULT VALUES:**
- When writing form-filling steps, ONLY include a field if entering a value is necessary to execute the test
- If a field has a default value that is acceptable for the scenario being tested, omit the step for that field entirely — do not instruct the tester to re-enter the default
- Entering a default value adds noise and makes tests harder to read without adding any verification value
❌ BAD: "Enter Seller Agent Fee: 3%" when 3% is the pre-populated default and the test is about VA-specific fees
✅ GOOD: Skip that step — the default is fine and the test is not about agent fees

**SCOPE INFERENCE FROM SECTION HEADINGS — CRITICAL:**
When a ticket uses a shared heading like "Defaults – Buyer & Seller" and lists items underneath it, DO NOT automatically assume every item applies to both Buyer AND Seller, or to a specific one of them.

Rules:
1. **Prefer code diffs over heading inference.** If diffs are provided, check whether the changed code touches buyer-specific paths, seller-specific paths, or both, for each individual item. Only generate tests for the contexts where the code actually changed.
2. **Do NOT split a single change into Buyer AND Seller tests unless the ticket explicitly says the change applies to both** (e.g., "apply to both buyer and seller defaults") or the diff confirms both paths were modified.
3. **Do NOT assign a Seller test to a feature/field that only appears in Buyer code** (or vice versa) even if the section heading mentions both.
4. If scope is genuinely ambiguous and no diff is available, use the most conservative interpretation: test the single combined flow described, and note the ambiguity in test_data rather than duplicating for each context.

❌ Ticket heading: "Defaults – Buyer & Seller" → Don't create a "Seller defaults" test for Hazard Insurance if Hazard Insurance only exists in Buyer defaults
✅ If diff shows HazardInsurance only changed in buyer-side files → generate test for Buyer defaults only
✅ If ticket explicitly says "applies to both buyer and seller" → generate tests for both

**IGNORE HISTORICAL / SUPERSEDED ACCEPTANCE CRITERIA:**
Ticket descriptions sometimes preserve old requirements under headings like "OG AC", "Old AC", "Original AC", "Previous AC", "Old Acceptance Criteria", or similar. These sections document what the requirements **used to be** — they are NOT current requirements.
- NEVER generate test cases based on content under these headings
- Treat them as historical context only; the current AC is everything outside those sections
❌ "OG AC: button should be red" → Do not test for a red button; the requirement has changed
✅ Look for the updated/current AC elsewhere in the description and test that instead

**WHEN TO ADD "ABSENCE" TESTS:**
Only test for the absence of something if:
- The ticket explicitly mentions removing/hiding a feature
- The PR changes show deletion of code related to that feature
- The ticket description specifically says "without X" or "don't include X"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ CRITICAL: NEVER USE REAL USERS AS TEST SUBJECTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Jira ticket context (description, comments, bounce history, PR discussion, Slack messages) frequently contains the names and email addresses of real customers, employees, reporters, and assignees. They appear there because they reported the bug, were affected by it, or discussed the fix — NOT because they are intended test subjects.

**NEVER write a test step that instructs a tester to log in as, impersonate, write data to, or trigger notifications for a real person mentioned in the ticket context.** This includes:
- Customer email addresses (anything that looks like `name@company.com` in a comment, description, or bounce reason)
- Customer/user names that appear alongside an email or are described as a reporter/affected user
- Internal employee names mentioned as the assignee, commenter, or developer
- Account IDs, user IDs, or other identifiers tied to a specific real person

**Why this matters:** Running write operations (uploading data, generating plans, triggering flows) against a real customer's account during QA can create production data they'll see, send them push notifications/emails, or collide with an in-flight fix on their account.

**What to do instead** — substitute a generic placeholder that describes the *role* or *configuration* required:
- ❌ BAD: `Log in as Lawrence (lawrence@modusrealestate.com)`
- ✅ GOOD: `Log in with a test account configured for the Empire Builder goal tier`
- ❌ BAD: `Verify Sarah's onboarding flow completes`
- ✅ GOOD: `Verify a test user's onboarding flow completes`
- ❌ BAD: `Replay the upload using jdoe@acme.com's profile`
- ✅ GOOD: `Replay the upload using a test profile with the same property type and state as the reported case`

**Read-only verification is the one exception** — it is acceptable to instruct the tester to *inspect* a real user's data (e.g., "verify in the admin panel that user X's duplicate rows were cleaned up") when the bug fix is specifically a backfill or repair for that user, AND the step does not write, modify, or trigger any action on the account. When in doubt, prefer a test account.

**When the ticket requires a specific configuration** that a real user happens to have (e.g., a particular goal tier, account state, or feature flag), describe the *configuration* in test_data and instruct the tester to use a test account matching it. Do not name the real user as the way to obtain that configuration.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ WHAT NOT TO TEST - BUILD-TIME vs RUNTIME
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**DO NOT CREATE TEST CASES FOR BUILD-TIME TOOLS OR CONFIGURATION:**
These are automatically validated by CI/build pipelines and don't require manual testing:

❌ ESLint configuration changes (eslint.config.js, .eslintrc, flat configs)
❌ Prettier/formatting configs
❌ TypeScript configuration (tsconfig.json compiler options)
❌ Build tool configs (webpack, vite, rollup, babel, esbuild)
❌ Package manager configs (package.json scripts, lockfiles, .npmrc)
❌ CI/CD pipeline configs (.github/workflows, .gitlab-ci.yml, Jenkinsfile)
❌ Development tooling (husky, lint-staged, commitlint)
❌ Test framework configs (jest.config.js, vitest.config.js)

**Why?** These fail the build automatically if broken. Manual testing adds no value.

**ONLY TEST RUNTIME BEHAVIOR:**
✅ App UI and functionality
✅ API endpoints and responses
✅ User authentication and authorization
✅ Data processing and validation
✅ Third-party integrations
✅ Performance and responsiveness
✅ Mobile/web app behavior on devices

**FOR SDK/DEPENDENCY UPDATES SPECIFICALLY:**
Focus on compatibility regression testing:
- Does the app still build and run?
- Do existing features still work with the new SDK version?
- Are there breaking changes from the SDK changelog that affect the app?

DO NOT test the features of the SDK itself - assume the SDK maintainers tested it.
DO NOT test that the build tools work - the build process itself validates this.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GENERATE TEST PLAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Create a clear, actionable test plan organized by feature/component. Extract requirements from any format provided and focus on functional testing from a user perspective. REMEMBER: Only test what is explicitly mentioned in the requirements above.

**ADJUST SCOPE BASED ON COMPLEXITY:**
Analyze the ticket complexity and adjust test coverage accordingly:

- **SDK/Dependency Updates** (React 18→19, Node 18→20, Expo 52→53, library upgrades):
  - 3-4 happy path tests (app launches, core flows work with new SDK version)
  - 2-4 edge cases (breaking changes from SDK changelog/migration guide)
  - 4-6 regression items (existing features unaffected by upgrade)
  - Focus on COMPATIBILITY testing, not testing SDK features themselves
  - DO NOT test build tools (ESLint, TypeScript configs, bundler settings)
  - Example: "App launches on iOS with Expo SDK 53" NOT "ESLint v9 validates code"

- **Simple Bug Fixes** (UI glitches, text changes, minor visual issues):
  - 2-3 happy path tests (verify fix works in main scenarios)
  - 2-3 edge cases (boundary conditions, theme switching)
  - 3-5 regression items (related features still work)
  - Skip integration tests unless system interaction is involved

- **Medium Features** (single component changes, API endpoints, form additions):
  - 3-5 happy path tests (cover main user flows)
  - 4-6 edge cases (error handling, validation, boundaries)
  - 5-8 regression items (impacted areas)
  - Include integration tests if APIs or multiple components involved

- **Complex Features** (multi-system changes, new workflows, security features):
  - 5-8 happy path tests (comprehensive flow coverage)
  - 6-10 edge cases (security, concurrency, data integrity)
  - 8-12 regression items (extensive impact analysis)
  - Include integration tests for system interactions

**ORGANIZE TESTS BY FEATURE/COMPONENT:**
Group related test cases logically by the feature or component they test.

**AVOID REDUNDANCY - CRITICAL RULE:**
DO NOT create separate test cases that test the same user flow from different angles. Instead, create ONE comprehensive test case that validates multiple aspects together.

**USE PRECONDITIONS TO ELIMINATE REPEATED SETUP STEPS:**
If 3 or more test cases in a section share the same opening steps (e.g. "Log in → navigate to listing → clear localStorage"), extract them into the `preconditions` field instead of repeating them in every steps list. Steps should begin at the actual variation point for that test.
- ❌ BAD: Every test starts with "Log in to Staging 2", "Navigate to a listing in an enabled state"
- ✅ GOOD: preconditions: "Logged in to Staging 2 with devagent account, on a listing in an enabled state"
- Only set preconditions when the setup is genuinely shared. If a test has unique setup (e.g. "clear localStorage" is only needed for 2 of 8 tests), keep those steps in the steps list.

**PARAMETERIZE IDENTICAL TEST CASES:**
If 3 or more test cases share identical steps and differ ONLY in test data (e.g. testing fallback behavior for states CT, KS, KY, LA), collapse them into ONE test case. Put the varying data in a markdown table inside the `test_data` field:
```
| State | Purchase Price |
|-------|---------------|
| CT    | $400,000      |
| KS    | $300,000      |
```
- ❌ BAD: Separate "Verify CT falls back gracefully", "Verify KS falls back gracefully", "Verify KY falls back gracefully" tests
- ✅ GOOD: One "Verify unsupported states fall back gracefully" test with a table of states in test_data

**USE TABLES FOR INPUT VALIDATION EDGE CASES:**
When multiple edge cases test the same input field with different invalid values (e.g. max boundary, negative, empty, decimal truncation), combine them into ONE test case. Use a markdown table in `test_data`:
```
| Input   | Trigger | Expected result     |
|---------|---------|---------------------|
| 25      | blur    | Clamped to 20%      |
| -5      | blur    | Restores to 6.75%   |
| (empty) | blur    | Restores to 6.75%   |
```
Only split into separate tests if the validation behavior is meaningfully different or requires different setup.

❌ BAD - Redundant tests:
  - Test 1: "User clicks button and modal appears"
  - Test 2: "Modal posts to correct API endpoint"
  - Test 3: "API response includes correct user context"

✅ GOOD - Single comprehensive test:
  - Test 1: "User clicks button, modal appears, posts to correct API endpoint with proper context"

Combine related validations when they're part of the same user flow. Only create separate tests when:
- Testing different user paths (thumbs up vs thumbs down)
- Testing different entry points (task completion vs chat completion)
- Testing truly independent functionality

**Before adding a test case, ask yourself:** "Does another test already cover this user flow?"
If yes, enhance that existing test instead of creating a new one.

**INCLUDE THESE TEST TYPES:**

1. **Positive Scenarios (Happy Path)**
   - Test complete user flows from start to finish
   - VALIDATE MULTIPLE ASPECTS IN ONE TEST: UI behavior, API correctness, AND data integrity
   - Each test should be a comprehensive end-to-end validation, not just a UI check
   - Use specific examples from the ticket (not generic placeholders)

2. **Negative Scenarios (Error Handling)** - ONLY for error/edge cases
   - Test with invalid inputs, missing data, unauthorized access
   - Verify proper error messages and recovery mechanisms
   - Include specific examples: invalid email formats, wrong passwords, etc.
   - These should test DIFFERENT scenarios, not the same flow with valid data

3. **Edge Cases (Boundary Conditions)** - ONLY for boundaries and unusual inputs
   - Test minimum/maximum values, empty states, special characters
   - Focus on input validation and boundary handling
   - Do NOT duplicate happy path flows here

4. **Integration Scenarios** - ONLY when testing multi-system interactions
   - Use ONLY when testing interactions between separate systems/services
   - Do NOT use for standard features where UI calls a single API
   - Examples: Cross-service data flow, third-party integrations, microservice communication
   - If it's just "frontend → single backend API → database", that's a normal flow (use happy_path)
   - **DEDUPLICATION RULE:** If a behavior (e.g., "PDF excludes branding when flag is off") is already verified through a UI-based happy path test, do NOT create a separate integration/API test for the same behavior. The UI test already exercises the underlying API. Only add an integration test when it covers a genuinely different scenario or system interaction that no UI test reaches.
   - **API verification steps MUST always specify HOW to verify:**
     - If there is a UI-observable outcome, describe it: "Verify the Transfer Tax field is NOT shown on screen"
     - If there is NO UI outcome (pure backend/response check), always provide explicit DevTools steps: "Open browser DevTools (F12) > Network tab > filter for '[endpoint-name]' > trigger the action > click the request > inspect the Response tab and confirm [specific field/value]"
   - ❌ NEVER write vague steps like "Verify the API returns filtered sections" or "Verify integrationInfo shows GFE status" — these are untestable without specifying the verification mechanism

5. **Reset/Clear Functionality**
   - Test any reset, clear, or undo operations
   - Verify data is properly cleared/restored

**FORMAT EACH TEST AS: ACTION → EXPECTED RESULT**
Each test should include:
- Clear action steps (what the user does)
- Expected result (what should happen)
- Specific test data when needed

**CRITICAL: STEP ORDERING RULES**
- Steps must be in the exact sequential order a user would perform them in the UI
- Form submission/action buttons (`calculate`, `submit`, `confirm`, `save`, etc.) MUST always come AFTER all required inputs have been filled in
- Never place an action button tap in the middle of filling out a form — fill ALL inputs first, then tap the action button
- Think through the complete user flow before writing steps: enter all inputs → then trigger the action

**ADDITIVE OPERATIONS — CRITICAL RULE:**
When a requirement says "add X to Y", "the difference is added to Y", "increase Y by X", or any similar additive phrasing, the test MUST verify accumulation, not replacement. A common implementation bug is to SET Y = X instead of SET Y = Y + X. If your test data doesn't include a pre-existing value for Y, this bug will pass undetected.

Rules:
1. **Always set a non-zero starting value** for any field that is being added to. Never let it default to zero or blank when testing an additive operation.
2. **State the exact expected total** in the expected result: `final_Y = initial_Y + X`. Do NOT write vague phrases like "the X amount is included" or "Y reflects the difference" — these pass even when the field was replaced instead of accumulated.
3. **Include the arithmetic explicitly** in test_data so the tester can verify without guessing: e.g. `Initial Down Payment: $A + Excess: $B = Expected Total: $A+B`. Always compute the actual sum from your chosen test values — never copy a sum from an example.

❌ BAD — misses the replace-vs-add bug:
- test_data: "Purchase Price: $600,000, Max FHA: $500,000"
- expected: "Down Payment includes the $100,000 difference"
  → Passes even if Down Payment is SET to $100,000 (replacing the original value)

✅ GOOD — catches the replace-vs-add bug:
- test_data: "Initial Down Payment: $50,000, Purchase Price: $600,000, Max FHA: $500,000. Expected: $50,000 + $100,000 = $150,000"
- expected: "Down Payment field shows $150,000 (original $50,000 + $100,000 excess). A result of $100,000 would indicate the original value was replaced instead of accumulated."

Apply this rule to ALL additive scenarios: down payment accumulation, running totals, fee stacking, counter increments, balance additions, etc.

**GUIDELINES:**
- Write from the user's perspective (avoid technical implementation details)
- Be specific and actionable (use concrete examples)
- Each test should be independently executable
- Include data validation testing when applicable
- Identify ambiguities or missing information when present
- Prioritize tests: critical > high > medium

**PRIORITY LEVELS:**
- "critical": Authentication, payments, data loss, security issues
- "high": Core functionality, common user flows, data integrity
- "medium": Edge cases, rare scenarios, minor issues

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE - GOOD TEST ORGANIZATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Scenario:** User feedback feature that posts to Slack

✅ CORRECT - 2 comprehensive tests in happy_path:
  1. "Complete thumbs up feedback flow with API validation"
     - Covers: UI modal appears → comment box displays → posts to correct endpoint → verifies Slack message contains user context
  2. "Complete thumbs down feedback flow with API validation"
     - Covers: UI modal appears → comment box displays → posts to correct endpoint → verifies Slack message contains user context

❌ INCORRECT - 6 redundant tests split across sections:
  Happy path:
    1. "Thumbs up displays comment box"
    2. "Thumbs down displays comment box"
  Edge cases:
    3. "Thumbs up posts to Slack"
    4. "Thumbs down posts to Slack"
  Integration tests:
    5. "Thumbs up uses correct API endpoint"
    6. "Thumbs down uses correct API endpoint"

The second approach tests the same flows 3 times each - wasteful and redundant!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return ONLY valid JSON (no markdown, no code blocks):

{
  "happy_path": [
    {
      "title": "Comprehensive test name covering the complete flow",
      "priority": "critical|high|medium",
      "preconditions": "Assumed pre-state shared with other tests (omit if unique to this test)",
      "steps": [
        "First action step (start here, not at login/navigation if covered by preconditions)",
        "Second action step - verify intermediate state",
        "Third action step",
        "Fourth step - verify API correctness",
        "Fifth step - verify data integrity and context"
      ],
      "expected": "Complete expected outcome covering UI behavior, API correctness, and data validation",
      "test_data": "All specific data needed for this comprehensive test"
    }
  ],
  "edge_cases": [
    {
      "title": "Clear test name for edge case or error scenario",
      "priority": "critical|high|medium",
      "category": "security|boundary|error_handling|integration",
      "steps": [
        "Setup step",
        "Action that triggers edge case",
        "Verification step"
      ],
      "expected": "Expected behavior (include error messages if applicable)",
      "test_data": "Specific edge case data (e.g., 'empty string', 'max+1 value: 101')"
    }
  ],
  "integration_tests": [
    {
      "title": "Test name for feature interaction or API test",
      "priority": "critical|high|medium",
      "steps": [
        "Setup multiple components",
        "Action involving multiple features",
        "Verify interaction"
      ],
      "expected": "Expected interaction result",
      "test_data": "Data needed for integration test"
    }
  ],
  "regression_checklist": [
    "🔴 Critical feature that must still work (be specific)",
    "🟡 Important related feature",
    "🟢 Additional validation item"
  ]
}

**REGRESSION CHECKLIST RULES:**
The regression checklist must contain ONLY runtime behaviors that can be manually tested.

❌ DO NOT INCLUDE build-time validations:
- "TypeScript compilation completes without errors"
- "ESLint validation passes"
- "Build succeeds with [SDK/tool version]"
- "App can be uploaded to App Store/Play Store"
- "No console warnings during build"

✅ ONLY INCLUDE runtime behaviors:
- "User authentication works correctly"
- "Navigation between screens functions properly"
- "Data saves and loads correctly"
- "Animations render smoothly"
- "API endpoints return expected data"

**Why?** Build-time checks fail automatically if broken. Regression checklists are for manually verifying existing features still work.

**CRITICAL ORDERING REQUIREMENT - READ THIS FIRST:**
YOU MUST ORDER ALL TEST CASES BY PRIORITY WITHIN EACH SECTION.
This is NON-NEGOTIABLE. The order MUST be:
  1. ALL "critical" priority tests FIRST
  2. ALL "high" priority tests SECOND
  3. ALL "medium" priority tests LAST

Apply this ordering to: happy_path, edge_cases, AND integration_tests sections.
DO NOT group tests by any other criteria (logical flow, dependencies, etc.).
PRIORITY ORDER OVERRIDES ALL OTHER CONSIDERATIONS.

Before generating each section, mentally sort your tests:
- Step 1: Identify all critical tests → put them first
- Step 2: Identify all high tests → put them after critical
- Step 3: Identify all medium tests → put them last

**RULES:**
- Steps array should contain plain action descriptions without numbering (numbering will be added during display)
- Priority values: "critical", "high", or "medium" (lowercase) - REQUIRED for all tests
- Categories: "security", "boundary", "error_handling", "integration"
- If integration_tests not needed, return empty array: []
- Use specific examples from the ticket, never generic placeholders
- All test_data should be concrete and specific

**FINAL CHECKLIST BEFORE GENERATING:**
✅ Every test case references something explicitly mentioned in the ticket/PR/context
✅ No tests for features that "should" exist but aren't actually mentioned
✅ No assumptions based on domain knowledge about what the application typically includes
✅ Tests are sorted by priority: critical → high → medium

Generate the test plan now. Remember: SORT BY PRIORITY FIRST and ONLY TEST WHAT IS EXPLICITLY MENTIONED."""


class LLMError(Exception):
    """Base exception for LLM-related errors."""

    def __init__(self, message: str, error_type: str = "service_unavailable") -> None:
        """
        Initialize LLMError.

        Args:
            message: Error message
            error_type: Type of error - "invalid", "expired", "rate_limited", "service_unavailable"
        """
        super().__init__(message)
        self.error_type = error_type


class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        images: list[tuple[str, str]] | None = None,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
        seed_regressions: list[dict] | None = None,
        bounce_history: list[dict] | None = None,
    ) -> TestPlan:
        """Generate a structured test plan from ticket data and context.

        Args:
            images: List of (base64_data, media_type) tuples for image analysis
            comments: List of filtered testing-related Jira comments
            slack_messages: Resolved Slack messages from permalinks found in ticket
            seed_regressions: Prior Bug Lens regression tests from sibling tickets
                under the same parent. Each dict: {source_ticket_keys, regression_tests, created_at}.
        """
        pass

    @abstractmethod
    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan from multiple related tickets.

        Args:
            tickets: List of ticket dicts with keys: ticket_key, summary, description,
                     issue_type, testing_context, development_info, comments,
                     parent_info, linked_info
            images: Combined image attachments from all tickets (up to 3)
        """
        pass

    @abstractmethod
    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
        status: str | None = None,
        status_category: str | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket and explain the bug, root cause, fix, and regression tests."""
        pass

    @abstractmethod
    async def generate_multi_bug_analysis(
        self,
        tickets: list[dict],
    ) -> BugAnalysis:
        """Analyze multiple related bug tickets together and produce a combined analysis."""
        pass

    @abstractmethod
    async def summarize_ticket(
        self,
        summary: str,
        description: str | None,
    ) -> str:
        """Return a 2-3 sentence plain-language summary of the ticket."""
        pass

    def _build_bug_analysis_prompt(self, tickets: list[dict]) -> str:
        """Build the prompt for bug analysis (single or multi-ticket)."""
        is_multi = len(tickets) > 1
        keys_str = ", ".join(t["ticket_key"] for t in tickets)

        if is_multi:
            prompt = f"Analyze the following {len(tickets)} related bug tickets together: {keys_str}.\n\n"
            prompt += "Produce a single combined analysis covering the shared root cause and fix.\n\n"
        else:
            ticket = tickets[0]
            prompt = f"Analyze this bug ticket: {ticket['ticket_key']}\n\n"

        for i, ticket in enumerate(tickets, 1):
            ticket_key = ticket["ticket_key"]
            summary = ticket["summary"]
            description = ticket.get("description")
            comments = ticket.get("comments")
            linked_info = ticket.get("linked_info")
            development_info = ticket.get("development_info")
            github_context = ticket.get("github_context")

            if is_multi:
                prompt += f"━━━ TICKET {i}: {ticket_key} ━━━\n"
            else:
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "TICKET INFORMATION\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            prompt += f"\n**Ticket:** {ticket_key}\n"
            prompt += f"**Summary:** {summary}\n"
            status_name = ticket.get("status")
            status_category = ticket.get("status_category")
            if status_name or status_category:
                cat_str = f" (category: {status_category})" if status_category else ""
                prompt += f"**Jira Status:** {status_name or 'unknown'}{cat_str}\n"
            prompt += f"\n**Description:**\n{description if description else 'No description provided'}\n"

            # Jira comments
            if comments:
                prompt += f"\n**Jira Comments ({len(comments)}):**\n"
                for comment in comments[:5]:
                    author = comment.get("author", "Unknown")
                    body = comment.get("body", "")
                    body_preview = body[:300] + "..." if len(body) > 300 else body
                    prompt += f"- @{author}: {body_preview}\n"

            # Linked issues (caused_by is most relevant for bugs)
            if linked_info:
                caused_by = linked_info.get("caused_by", [])
                if caused_by:
                    prompt += f"\n**Caused By:**\n"
                    for issue in caused_by:
                        prompt += f"- {issue.get('key')}: {issue.get('summary')}\n"

            # Development info (PRs + diffs — the heart of the analysis)
            if development_info:
                pull_requests = development_info.get("pull_requests", [])
                if pull_requests:
                    prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "PULL REQUESTS & CODE CHANGES\n"
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    open_prs = [pr for pr in pull_requests if (pr.get("status") or "").upper() == "OPEN"]
                    if open_prs:
                        prompt += (
                            f"\n⚠️ {len(open_prs)} of {len(pull_requests)} PR(s) are still OPEN — "
                            "the code in those PRs is unmerged and may still change. "
                            "Note this when reasoning about the fix state.\n"
                        )
                    for pr in pull_requests:
                        status = pr.get("status", "UNKNOWN")
                        merged = status.upper() in ("MERGED", "CLOSED")
                        prompt += f"\n**PR:** {pr.get('title', 'Untitled')} — Status: {status}"
                        if merged:
                            prompt += " ✅ (merged — code change is in)"
                        prompt += "\n"
                        if pr.get("source_branch"):
                            prompt += f"Branch: {pr['source_branch']}\n"
                        if pr.get("github_description"):
                            desc = pr["github_description"]
                            prompt += f"PR Description: {desc[:1000] + '...' if len(desc) > 1000 else desc}\n"

                        files_changed = pr.get("files_changed")
                        if files_changed:
                            sorted_files = sorted(files_changed, key=lambda f: f.get("changes", 0), reverse=True)
                            prompt += f"\nFiles changed ({len(files_changed)}):\n"
                            for fc in sorted_files[:15]:
                                icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(fc.get("status", ""), "📄")
                                prompt += f"  {icon} {fc.get('filename', 'unknown')} (+{fc.get('additions', 0)}/-{fc.get('deletions', 0)})\n"

                            files_with_patches = [f for f in sorted_files if f.get("patch")]
                            if files_with_patches:
                                prompt += "\nCode diffs (use these to identify root cause and explain the fix):\n"
                                total_chars = 0
                                MAX_TOTAL = 16000
                                MAX_PER_FILE = 4000
                                for fc in files_with_patches:
                                    if total_chars >= MAX_TOTAL:
                                        break
                                    patch = fc.get("patch", "")
                                    fname = fc.get("filename", "unknown")
                                    if len(patch) > MAX_PER_FILE:
                                        patch = patch[:MAX_PER_FILE] + "\n...(truncated)"
                                    remaining = MAX_TOTAL - total_chars
                                    if len(patch) > remaining:
                                        patch = patch[:remaining] + "\n...(truncated)"
                                    prompt += f"\n--- {fname} ---\n"
                                    for line in patch.split("\n"):
                                        prompt += f"  {line}\n"
                                    total_chars += len(patch)

                        pr_comments = pr.get("comments")
                        if pr_comments:
                            prompt += f"\nPR Discussion ({len(pr_comments)} comments):\n"
                            for comment in pr_comments[:8]:
                                body = comment.get("body", "")
                                body_preview = body[:200] + "..." if len(body) > 200 else body
                                icon = "📝" if comment.get("comment_type") == "review_comment" else "💬"
                                prompt += f"  {icon} @{comment.get('author', 'unknown')}: {body_preview}\n"

            # GitHub context fetched from links in the ticket body
            if github_context:
                prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "LINKED CODE CONTEXT (fetched from GitHub links in the ticket)\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "Use this code to inform root cause identification and fix complexity.\n\n"
                for item in github_context:
                    if item.get("type") == "file":
                        label = f"{item['path']}"
                        if item.get("lines"):
                            label += f" ({item['lines']})"
                        label += f" @ {item['ref']}"
                        prompt += f"**File: {label}**\n```\n{item['content']}\n```\n\n"
                    elif item.get("type") == "commit":
                        prompt += f"**Commit {item['sha']}:** {item['message']}\n"
                        for f in item.get("files", []):
                            icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(f.get("status", ""), "📄")
                            prompt += f"  {icon} {f['filename']} (+{f['additions']}/-{f['deletions']})\n"
                            if f.get("patch"):
                                prompt += f"```diff\n{f['patch']}\n```\n"
                        prompt += "\n"

            prompt += "\n"

        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        prompt += "Now submit your bug analysis using the submit_bug_analysis tool.\n"
        return prompt

    def _build_prompt(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        has_images: bool = False,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
        seed_regressions: list[dict] | None = None,
        bounce_history: list[dict] | None = None,
    ) -> str:
        """Build the prompt for test plan generation (shared across providers)."""
        prompt = f"""**Your Task:** Create a detailed test plan for the following Jira ticket{" (screenshots/mockups attached)" if has_images else ""}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TICKET INFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**Ticket:** {ticket_key}
**Summary:** {summary}

**Description:**
{description if description else "No description provided"}
"""

        # Add parent ticket context if available
        if parent_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PARENT TICKET CONTEXT\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\n**This is a sub-task of:** {parent_info.get('key')} - {parent_info.get('summary')}\n"
            prompt += f"**Parent Type:** {parent_info.get('issue_type')}\n"

            parent_desc = parent_info.get('description')
            if parent_desc:
                # Truncate parent description if too long
                desc_preview = parent_desc[:1000] + "..." if len(parent_desc) > 1000 else parent_desc
                prompt += f"\n**Parent Description:**\n{desc_preview}\n"

            # Highlight parent resources
            parent_resources = []
            if parent_info.get('figma_context'):
                figma = parent_info['figma_context']
                file_name = _safe_get(figma, 'file_name', 'Unknown')
                parent_resources.append(f"📐 Figma design: {file_name}")
            if parent_info.get('attachments'):
                attachment_count = len(parent_info['attachments'])
                parent_resources.append(f"🖼️ {attachment_count} design image{'s' if attachment_count > 1 else ''}")

            if parent_resources:
                prompt += f"\n**Parent Resources:**\n"
                for resource in parent_resources:
                    prompt += f"- {resource}\n"

            prompt += "\n**Use parent context to:**\n"
            prompt += "- Understand the overall feature/epic this sub-task contributes to\n"
            prompt += "- Align test scenarios with parent-level business requirements and acceptance criteria\n"
            prompt += "- Use design specifications from parent Figma files and mockups\n"
            prompt += "- Validate that this sub-task fulfills its role in the broader feature\n"
            prompt += "- Consider integration points with other sub-tasks under the same parent\n"

        # Add linked issues context if available
        if linked_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "LINKED ISSUES (DEPENDENCIES)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            # Show blocked_by issues (highest priority - these must be done first)
            blocked_by = linked_info.get('blocked_by', [])
            if blocked_by:
                prompt += f"\n**⛔ Blocked By ({len(blocked_by)} issue{'s' if len(blocked_by) > 1 else ''}):**\n"
                prompt += "This ticket CANNOT be tested until these are resolved:\n\n"
                for issue in blocked_by:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('status'):
                        prompt += f"  Status: {issue.get('status')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show blocks issues (test carefully - don't break downstream work)
            blocks = linked_info.get('blocks', [])
            if blocks:
                prompt += f"\n**🔒 Blocks ({len(blocks)} issue{'s' if len(blocks) > 1 else ''}):**\n"
                prompt += "This ticket blocks these downstream tickets - test thoroughly:\n\n"
                for issue in blocks:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('status'):
                        prompt += f"  Status: {issue.get('status')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show caused_by issues (root cause context)
            caused_by = linked_info.get('caused_by', [])
            if caused_by:
                prompt += f"\n**🐛 Caused By ({len(caused_by)} issue{'s' if len(caused_by) > 1 else ''}):**\n"
                prompt += "Root cause issues that led to this ticket:\n\n"
                for issue in caused_by:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            # Show causes issues (validate the fix doesn't cause downstream issues)
            causes = linked_info.get('causes', [])
            if causes:
                prompt += f"\n**⚠️ Causes ({len(causes)} issue{'s' if len(causes) > 1 else ''}):**\n"
                prompt += "This ticket may cause these issues - validate fixes don't regress:\n\n"
                for issue in causes:
                    prompt += f"- **{issue.get('key')}**: {issue.get('summary')}\n"
                    if issue.get('description'):
                        desc_preview = issue['description'][:200] + "..." if len(issue['description']) > 200 else issue['description']
                        prompt += f"  Description: {desc_preview}\n"
                    prompt += "\n"

            prompt += "**Use linked issues to:**\n"
            if blocked_by:
                prompt += "- ⚠️ CRITICAL: Validate that blocking issues are resolved before testing\n"
                prompt += "- Understand prerequisites and API contracts from blocking tickets\n"
            if blocks:
                prompt += "- Test thoroughly - downstream work depends on this being correct\n"
                prompt += "- Consider how changes might affect dependent tickets\n"
            if caused_by:
                prompt += "- Ensure the root cause is actually fixed, not just symptoms\n"
            if causes:
                prompt += "- Validate that fixes don't introduce regressions in related areas\n"
            prompt += "- Test integration points between this ticket and linked dependencies\n"

        # Add Jira comments if available
        if comments:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "JIRA COMMENTS (TESTING-RELATED)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\nThe following {len(comments)} comment(s) from the Jira ticket contain testing discussions, edge cases, or scenarios:\n\n"

            for i, comment in enumerate(comments, 1):
                author = comment.get('author', 'Unknown')
                body = comment.get('body', '')
                created = comment.get('created', '')

                # Allow up to 8000 chars per comment to preserve full manual test plans.
                # Structured test plans written by developers can be several thousand chars
                # and must not be truncated, as every test case is meaningful.
                body_preview = body[:8000] + "..." if len(body) > 8000 else body

                prompt += f"**Comment {i} by {author}** (Posted: {created[:10] if created else 'Unknown date'}):\n"
                prompt += f"{body_preview}\n\n"

            prompt += "**Use these comments to:**\n"
            prompt += "- If a manual test plan is present, use it as the primary source of truth for test cases — preserve its structure, numbering, and coverage\n"
            prompt += "- Incorporate manually suggested test scenarios and edge cases\n"
            prompt += "- Address specific concerns or questions raised about testing\n"
            prompt += "- Include validation steps mentioned in the discussions\n"
            prompt += "- Consider any reproduction steps or test data mentioned\n\n"

        # Add resolved Slack discussions if available
        if slack_messages:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "SLACK DISCUSSIONS (LINKED IN TICKET)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"\nThe following {len(slack_messages)} Slack message(s) were linked from the ticket description or comments:\n\n"

            for i, msg in enumerate(slack_messages[:10], 1):
                author = _safe_get(msg, "author", "Unknown") or "Unknown"
                text = _safe_get(msg, "text", "") or ""
                url = _safe_get(msg, "url", "")
                # Cap each message to keep prompt size predictable; callers should
                # still include the URL so testers can read the full thread if needed.
                text_preview = text[:2000] + "..." if len(text) > 2000 else text
                prompt += f"**Slack message {i} by {author}:**\n"
                prompt += f"{text_preview}\n"
                if url:
                    prompt += f"(source: {url})\n"
                prompt += "\n"

            prompt += "**Use these Slack messages to:**\n"
            prompt += "- Incorporate edge cases, scenarios, or constraints raised in discussion\n"
            prompt += "- Treat them as supplementary context; the ticket itself remains the source of truth\n\n"

        if seed_regressions:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PRIOR REGRESSION TESTS (FROM RELATED BUG LENS ANALYSES)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += (
                "\nThe following regression tests were proposed by Bug Lens for prior "
                "tickets under the same parent/Epic. Treat them as candidate seeds for "
                "this ticket's regression coverage:\n\n"
            )
            for entry in seed_regressions[:5]:
                src = ", ".join(entry.get("source_ticket_keys") or [])
                tests = entry.get("regression_tests") or []
                prompt += f"**From {src}:**\n"
                for t in tests[:8]:
                    if isinstance(t, str) and t.strip():
                        prompt += f"- {t.strip()}\n"
                prompt += "\n"
            prompt += "**How to use these:**\n"
            prompt += "- Include any that remain relevant to this ticket's surface area, adapted as needed for this ticket's specifics.\n"
            prompt += "- Skip ones that are clearly unrelated to this ticket's behavior.\n"
            prompt += "- Prefer adding them under the regression checklist rather than happy-path or edge cases.\n\n"

        if bounce_history:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PRIOR QA / UAT BOUNCE-BACK HISTORY\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += (
                "\nThis ticket was previously moved forward (e.g. to QA, UAT, or Testing) "
                "and then sent back to an earlier workflow state. Each entry below is a "
                "regression-prone moment that the test plan MUST cover explicitly.\n\n"
            )
            for i, b in enumerate(bounce_history[:5], 1):
                from_status = b.get("from_status") or "?"
                to_status = b.get("to_status") or "?"
                prompt += f"**Bounce {i}:** {from_status} → {to_status}\n"
                ts = b.get("timestamp")
                if ts:
                    prompt += f"  When: {ts[:10]}\n"
                if b.get("author"):
                    prompt += f"  Moved by: {b['author']}\n"
                reason = b.get("reason")
                if reason:
                    prompt += f"  Reported reason:\n  > {reason}\n"
                else:
                    prompt += "  (No comment found near the transition — reason unknown.)\n"
                prompt += "\n"
            prompt += "**How to use this history:**\n"
            prompt += "- For each bounce, write at least one explicit regression test case that exercises the failure mode the PM described.\n"
            prompt += "- If the reason is vague (e.g. 'doesn't work'), add tests that walk the previously-failing flow end-to-end with realistic data.\n"
            prompt += "- Place these under the regression checklist and edge cases sections — they are the highest-priority coverage for this ticket.\n\n"

        if has_images:
            prompt += "\n**Note:** Screenshots or mockups are attached. Use them to understand the UI requirements and generate specific visual test cases.\n"

        # Add repository context if available (Phase 4: Repository Documentation)
        if development_info and development_info.get("repository_context"):
            repo_context = development_info["repository_context"]
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "PROJECT DOCUMENTATION\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

            readme = repo_context.get("readme_content")
            if readme:
                # Include README (truncate if very long)
                readme_preview = readme[:2000] + "..." if len(readme) > 2000 else readme
                prompt += f"\n**README.md:**\n{readme_preview}\n"

            test_examples = repo_context.get("test_examples")
            if test_examples:
                prompt += f"\n**Test File Examples Found:**\n"
                for test_file in test_examples[:5]:
                    prompt += f"- {test_file}\n"
                prompt += "\nUse these test patterns and project documentation to generate specific test cases that match this project's structure and conventions.\n"

        # Add UI/simulator testing context if available.
        # These files are present on repos that use the simulator-testing skill pattern
        # (e.g. agent-calculator). Silently skipped for repos that don't have them.
        if development_info and development_info.get("repository_context"):
            repo_context = development_info["repository_context"]
            screen_guide = repo_context.get("screen_guide")
            testid_reference = repo_context.get("testid_reference")

            if screen_guide or testid_reference:
                prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "UI NAVIGATION CONTEXT\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "\nThis app has stable testID identifiers on every interactive element. "
                prompt += "Use these in your test steps instead of generic descriptions.\n"
                prompt += "Example: write 'tap `price-input`' not 'tap the price field'.\n"

                if screen_guide:
                    # Include the navigation structure + first portion of screen descriptions.
                    # The full guide can be long; 4000 chars covers the structure overview
                    # and the most-tested screens.
                    guide_preview = screen_guide[:4000] + "\n...(truncated)" if len(screen_guide) > 4000 else screen_guide
                    prompt += f"\n**Screen Navigation Guide:**\n{guide_preview}\n"

                if testid_reference:
                    # Full reference maps every testID to its screen.
                    # 5000 chars covers the vast majority of screens.
                    ref_preview = testid_reference[:5000] + "\n...(truncated)" if len(testid_reference) > 5000 else testid_reference
                    prompt += f"\n**Available TestIDs by Screen:**\n{ref_preview}\n"

                prompt += "\n**Rules when using this context:**\n"
                prompt += "- Reference testIDs with backticks in action steps: `button-testid`\n"
                prompt += "- Only reference testIDs that appear in the list above\n"
                prompt += "- Use exact screen names from the guide for navigation steps\n"
                prompt += "- If a flow requires screens not in the guide, describe them generically\n"
                prompt += "- ⚠️ THE TESTID REFERENCE IS EXHAUSTIVE: every interactive element in the app has a testID listed above. If a form field or button does NOT appear in the reference, it does not exist in this app — do NOT invent steps for it, regardless of what domain knowledge suggests.\n"
                prompt += "- ⚠️ FORM FIELD COMPLETENESS: when writing form-filling steps, cross-check EVERY field against the testID reference. If you cannot find a matching testID for a field you are about to include, omit that step entirely.\n"

        # Add Figma design context if available (Phase 5)
        if development_info and development_info.get("figma_context"):
            figma_context = development_info["figma_context"]
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "DESIGN SPECIFICATIONS (FIGMA)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            file_name = _safe_get(figma_context, 'file_name', 'Unknown')
            prompt += f"\n**Design File:** {file_name}\n"

            # Add frames/screens (limit to 30)
            frames = _safe_get(figma_context, "frames", [])
            if frames:
                prompt += f"\n**Screens/Frames ({len(frames)}):**\n"
                for frame in frames[:30]:
                    frame_name = _safe_get(frame, "name", "Unknown")
                    frame_type = _safe_get(frame, "type", "FRAME")
                    prompt += f"- {frame_name} ({frame_type})\n"

            # Add components (limit to 20)
            components = _safe_get(figma_context, "components", [])
            if components:
                prompt += f"\n**UI Components ({len(components)}):**\n"
                for comp in components[:20]:
                    comp_name = _safe_get(comp, "name", "Unknown")
                    comp_desc = _safe_get(comp, "description", None)
                    comp_info = f"- {comp_name}"
                    if comp_desc:
                        comp_info += f": {comp_desc}"
                    prompt += comp_info + "\n"

            prompt += "\n**Use this design context to:**\n"
            prompt += "- Reference actual screen names and UI component names from Figma\n"
            prompt += "- Generate UI-specific test cases using exact component names\n"
            prompt += "- Create visual validation tests for each screen/frame\n"
            prompt += "- Ensure test steps match the design specifications\n"

        # Add development information if available
        if development_info:
            prompt += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "DEVELOPMENT ACTIVITY\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "\nThe following development work has been completed for this ticket:\n"

            pull_requests = development_info.get("pull_requests", [])
            if pull_requests:
                prompt += f"\n**Pull Requests ({len(pull_requests)}):**\n"
                for pr in pull_requests:
                    prompt += f"- **{pr.get('title', 'Untitled PR')}** (Status: {pr.get('status', 'UNKNOWN')})\n"
                    if pr.get('source_branch'):
                        prompt += f"  Branch: {pr.get('source_branch')}\n"

                    # Add GitHub PR description if available (Phase 3a)
                    gh_desc = pr.get('github_description')
                    if gh_desc:
                        # Truncate long descriptions
                        desc_preview = gh_desc[:1500] + "..." if len(gh_desc) > 1500 else gh_desc
                        prompt += f"  PR Description: {desc_preview}\n"

                    # Add code changes summary if available (Phase 3a)
                    files_changed = pr.get('files_changed')
                    if files_changed:
                        total_additions = pr.get('total_additions', 0)
                        total_deletions = pr.get('total_deletions', 0)
                        prompt += f"  📊 Code Changes: {len(files_changed)} files modified (+{total_additions}/-{total_deletions})\n"

                        # Show modified files (limit to 15 most significant)
                        prompt += "  📁 Modified Files:\n"
                        sorted_files = sorted(files_changed, key=lambda f: f.get('changes', 0), reverse=True)
                        for file_change in sorted_files[:15]:
                            filename = file_change.get('filename', 'unknown')
                            status = file_change.get('status', 'modified')
                            additions = file_change.get('additions', 0)
                            deletions = file_change.get('deletions', 0)

                            status_icon = {
                                "added": "✨",
                                "modified": "📝",
                                "removed": "🗑️",
                                "renamed": "📛",
                            }.get(status, "📄")

                            prompt += f"     {status_icon} {filename} (+{additions}/-{deletions})\n"

                        if len(files_changed) > 15:
                            prompt += f"     ... and {len(files_changed) - 15} more files\n"

                        # Show actual diff patches for runtime source files.
                        # Capped at 16000 chars total / 4000 chars per file so the prompt
                        # stays manageable while still exposing what was actually implemented.
                        files_with_patches = [f for f in sorted_files if f.get('patch')]
                        if files_with_patches:
                            prompt += "\n  📋 Key Code Changes (runtime files only):\n"
                            total_patch_chars = 0
                            MAX_TOTAL = 16000
                            MAX_PER_FILE = 4000
                            for fc in files_with_patches:
                                if total_patch_chars >= MAX_TOTAL:
                                    break
                                patch = fc.get('patch', '')
                                fname = fc.get('filename', 'unknown')
                                if len(patch) > MAX_PER_FILE:
                                    patch = patch[:MAX_PER_FILE] + "\n     ...(truncated)"
                                remaining = MAX_TOTAL - total_patch_chars
                                if len(patch) > remaining:
                                    patch = patch[:remaining] + "\n     ...(truncated)"
                                prompt += f"\n  --- {fname} ---\n"
                                for line in patch.split('\n'):
                                    prompt += f"  {line}\n"
                                total_patch_chars += len(patch)
                            prompt += "\n  ⚠️ REQUIRED: Read these diffs carefully and generate test cases for every new behaviour they introduce — especially new data sources, new fields, new API calls, and new conditional logic.\n"

                        prompt += "\n"

                    # Add PR comments if available (Phase 3b)
                    comments = pr.get('comments')
                    if comments:
                        prompt += f"  💬 PR Discussion ({len(comments)} comments):\n"
                        # Show most recent/relevant comments (limit to 10)
                        for comment in comments[:10]:
                            author = comment.get('author', 'unknown')
                            body = comment.get('body', '')
                            comment_type = comment.get('comment_type', 'conversation')

                            # Truncate long comments
                            body_preview = body[:200] + "..." if len(body) > 200 else body

                            # Format differently for review comments (they have file context)
                            icon = "📝" if comment_type == "review_comment" else "💬"
                            prompt += f"     {icon} @{author}: {body_preview}\n"

                        if len(comments) > 10:
                            prompt += f"     ... and {len(comments) - 10} more comments\n"

                        prompt += "\n"
                        prompt += "  ⚠️ REQUIRED: Generate specific test cases from the PR discussion above:\n"
                        prompt += "     - Each concern or question raised by a reviewer → create a test case that validates it\n"
                        prompt += "     - Each edge case or gotcha mentioned → create a test case that exercises it\n"
                        prompt += "     - Each bug or unexpected behavior noted → create a test case that catches regression\n"

            # Add commit information
            commits = development_info.get("commits", [])
            if commits:
                prompt += f"\n**Commits ({len(commits)}):**\n"
                # Show first 10 commit messages to avoid overwhelming the prompt
                for commit in commits[:10]:
                    commit_msg = commit.get('message', 'No message').split('\n')[0]  # First line only
                    author = commit.get('author', 'Unknown')
                    prompt += f"- {commit_msg} (by {author})\n"
                if len(commits) > 10:
                    prompt += f"... and {len(commits) - 10} more commits\n"

            # Add branch information
            branches = development_info.get("branches", [])
            if branches:
                prompt += f"\n**Branches:**\n"
                for branch in branches:
                    prompt += f"- {branch}\n"

            prompt += "\n**Use this development context to:**\n"
            prompt += "- Understand the project structure and architecture from the README documentation\n"
            prompt += "- Use project-specific terminology, UI component names, and navigation patterns from the documentation\n"
            prompt += "- Generate test steps with actual screen names, button labels, and menu items grounded in the PR diff or testID reference (see 'UI GROUNDING' below). If you can't find a UI element in either source, do not invent the label — flag it in `grounding_warnings`.\n"
            prompt += "- Infer what functionality was implemented from commit messages and PR titles\n"
            prompt += "- Analyze the modified files to identify which components/modules were changed\n"
            prompt += "- **FILTER OUT build-time changes**: Ignore ESLint configs, TypeScript configs, build tool settings, CI configs - focus ONLY on runtime code (UI components, API logic, business logic, data models)\n"
            prompt += "- Extract edge cases and gotchas mentioned in PR comments and code review discussions\n"
            prompt += "- Identify concerns, bugs, or scenarios discussed by developers during code review\n"
            prompt += "- Identify potential risk areas based on the type and scope of code changes\n"
            prompt += "- Generate specific test cases targeting the modified files and their dependencies\n"
            prompt += "- Focus testing on high-risk areas (authentication, payments, data handling, etc.)\n"
            prompt += "- Consider edge cases related to the specific code changes made\n"

        # Add user-provided context if available
        if testing_context.get("acceptanceCriteria"):
            prompt += f"\n**Acceptance Criteria:**\n{testing_context['acceptanceCriteria']}\n"

        if testing_context.get("specialInstructions"):
            prompt += f"\n**Special Testing Instructions:**\n{testing_context['specialInstructions']}\n"

        if _is_voice_ticket(summary, description):
            prompt += VOICE_TESTING_GUIDANCE

        if _is_observability_ticket(summary, description):
            prompt += OBSERVABILITY_TESTING_GUIDANCE

        prompt += UI_GROUNDING_GUIDANCE

        return prompt

    def _build_multi_ticket_prompt(
        self,
        tickets: list[dict],
        has_images: bool = False,
    ) -> str:
        """Build a combined prompt for multiple related tickets sharing code changes.

        Tickets are reordered newest-first by the numeric suffix of the ticket
        key (e.g. SK-2194 before SK-2138) so the conflict-resolution rule
        ("newer ticket wins on AC conflicts") is unambiguous to both the LLM
        and any human reading the prompt log.
        """
        tickets = _sort_tickets_newest_first(tickets)
        ticket_keys = [t["ticket_key"] for t in tickets]
        keys_str = ", ".join(ticket_keys)
        recency_str = " > ".join(ticket_keys)  # newest > … > oldest

        prompt = f"""**Your Task:** Create a single, unified, deduplicated test plan covering the following related Jira tickets that share code changes: {keys_str}.{"  (screenshots/mockups attached)" if has_images else ""}

Treat all tickets as parts of one combined feature. Do NOT produce separate test plans — generate ONE plan that covers the full scope.

"""

        # ── AC coverage matrix (must come BEFORE per-ticket details) ─────────
        # Build a flat list of (ac_id, ac_text) the LLM must cover.
        ac_index: list[tuple[str, str]] = []  # [(ac_id, text), ...]
        per_ticket_acs: dict[str, list[tuple[str, str]]] = {}
        for ticket in tickets:
            key = ticket["ticket_key"]
            acs = ticket.get("acceptance_criteria") or []
            entries = [(f"{key}-AC{i}", ac) for i, ac in enumerate(acs, 1)]
            per_ticket_acs[key] = entries
            ac_index.extend(entries)

        if ac_index:
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "ACCEPTANCE CRITERIA TO COVER (every ID below must appear in ≥1 test case's `covers_acs`)\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for key, entries in per_ticket_acs.items():
                if not entries:
                    continue
                prompt += f"**{key}:**\n"
                for ac_id, text in entries:
                    prompt += f"- {ac_id}: {text}\n"
                prompt += "\n"
            prompt += (
                "Every AC ID above must appear in the `covers_acs` field of at least one test case "
                "(happy_path, edge_cases, or integration_tests). If a single test legitimately "
                "exercises multiple ACs, list all of their IDs. Do NOT drop ACs to reduce duplication.\n\n"
            )

            # ── Conflict resolution: newer ticket wins ──────────────────────
            if len(tickets) > 1:
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                prompt += "AC CONFLICT RESOLUTION — NEWER TICKET WINS\n"
                prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                prompt += f"Ticket recency (newest → oldest): {recency_str}\n\n"
                prompt += (
                    "Two ACs from different tickets in this batch may describe the *same observable behaviour* "
                    "with *different requirements* (e.g. SK-2138-AC3 says 'modal stays open after Save' but "
                    "SK-2194-AC1 says 'modal closes after Save'). When that happens:\n\n"
                    "- The newer ticket's AC is the source of truth — write tests against IT, not the older one.\n"
                    "- Do NOT add the older (superseded) AC ID to any test case's `covers_acs`. It is overridden.\n"
                    "- Do NOT write a separate test for the older AC's behaviour — that behaviour no longer applies.\n"
                    "- Report the override in the top-level `superseded_acs` array: "
                    "`{loser_id: '<older AC ID>', winner_id: '<newer AC ID>', reason: '<one sentence>'}`.\n\n"
                    "Only flag a conflict when two ACs are about the *same observable behaviour* and *disagree*. "
                    "ACs that describe *different* behaviours are not conflicts — both must be tested.\n"
                    "ACs that say the *same* thing in different words are duplicates, not conflicts — cover them "
                    "with one test that tags both IDs in `covers_acs` (don't put them in `superseded_acs`).\n\n"
                )

        # ── Per-ticket summaries ──────────────────────────────────────────────
        for i, ticket in enumerate(tickets, 1):
            ticket_key = ticket["ticket_key"]
            summary = ticket["summary"]
            description = ticket.get("description")

            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += f"TICKET {i} OF {len(tickets)}: {ticket_key}\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            prompt += f"**Summary:** {summary}\n\n"

            ticket_acs = per_ticket_acs.get(ticket_key) or []
            if ticket_acs:
                prompt += "**Acceptance Criteria:**\n"
                for ac_id, text in ticket_acs:
                    prompt += f"- {ac_id}: {text}\n"
                prompt += "\n"

            prompt += f"**Description:**\n{description if description else 'No description provided'}\n"

            parent_info = ticket.get("parent_info")
            if parent_info:
                prompt += f"\n**Parent Ticket:** {parent_info.get('key')} — {parent_info.get('summary')}\n"

            linked_info = ticket.get("linked_info")
            if linked_info:
                blocked_by = linked_info.get("blocked_by", [])
                if blocked_by:
                    prompt += f"**Blocked By:** {', '.join(b['key'] for b in blocked_by)}\n"

            comments = ticket.get("comments")
            if comments:
                prompt += f"\n**Testing Comments ({len(comments)}):**\n"
                for comment in comments[:3]:
                    body = comment.get("body", "")
                    body_preview = body[:300] + "..." if len(body) > 300 else body
                    prompt += f"- @{comment.get('author', 'Unknown')}: {body_preview}\n"

            prompt += "\n"

        # ── Shared development activity ───────────────────────────────────────
        tickets_with_dev = [t for t in tickets if t.get("development_info")]
        if tickets_with_dev:
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            prompt += "SHARED DEVELOPMENT ACTIVITY\n"
            prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            for ticket in tickets_with_dev:
                dev_info = ticket["development_info"]
                ticket_key = ticket["ticket_key"]
                prompt += f"**{ticket_key} — Development:**\n"

                pull_requests = dev_info.get("pull_requests", [])
                for pr in pull_requests:
                    prompt += f"- PR: **{pr.get('title', 'Untitled')}** ({pr.get('status', 'UNKNOWN')})\n"
                    if pr.get("source_branch"):
                        prompt += f"  Branch: {pr['source_branch']}\n"
                    if pr.get("github_description"):
                        desc = pr["github_description"]
                        prompt += f"  PR Description: {desc[:200] + '...' if len(desc) > 200 else desc}\n"

                    files_changed = pr.get("files_changed")
                    if files_changed:
                        total_add = pr.get("total_additions", 0)
                        total_del = pr.get("total_deletions", 0)
                        prompt += f"  📊 {len(files_changed)} files (+{total_add}/-{total_del})\n"
                        sorted_files = sorted(files_changed, key=lambda f: f.get("changes", 0), reverse=True)
                        prompt += "  📁 Files:\n"
                        for fc in sorted_files[:10]:
                            icon = {"added": "✨", "modified": "📝", "removed": "🗑️", "renamed": "📛"}.get(fc.get("status", ""), "📄")
                            prompt += f"     {icon} {fc.get('filename', 'unknown')} (+{fc.get('additions', 0)}/-{fc.get('deletions', 0)})\n"
                        if len(files_changed) > 10:
                            prompt += f"     ... and {len(files_changed) - 10} more files\n"

                        # Code diffs — smaller budget per ticket in multi-ticket mode
                        files_with_patches = [f for f in sorted_files if f.get("patch")]
                        if files_with_patches:
                            prompt += "\n  📋 Key Code Changes:\n"
                            total_patch_chars = 0
                            MAX_TOTAL = 8000
                            MAX_PER_FILE = 2000
                            for fc in files_with_patches:
                                if total_patch_chars >= MAX_TOTAL:
                                    break
                                patch = fc.get("patch", "")
                                fname = fc.get("filename", "unknown")
                                if len(patch) > MAX_PER_FILE:
                                    patch = patch[:MAX_PER_FILE] + "\n     ...(truncated)"
                                remaining = MAX_TOTAL - total_patch_chars
                                if len(patch) > remaining:
                                    patch = patch[:remaining] + "\n     ...(truncated)"
                                prompt += f"\n  --- {fname} ---\n"
                                for line in patch.split("\n"):
                                    prompt += f"  {line}\n"
                                total_patch_chars += len(patch)
                            prompt += "\n  ⚠️ REQUIRED: Read these diffs and generate test cases for every new behaviour introduced.\n"

                        prompt += "\n"

                    pr_comments = pr.get("comments")
                    if pr_comments:
                        prompt += f"  💬 PR Discussion ({len(pr_comments)} comments):\n"
                        for comment in pr_comments[:5]:
                            body = comment.get("body", "")
                            body_preview = body[:150] + "..." if len(body) > 150 else body
                            icon = "📝" if comment.get("comment_type") == "review_comment" else "💬"
                            prompt += f"     {icon} @{comment.get('author', 'unknown')}: {body_preview}\n"
                        prompt += "\n"

                commits = dev_info.get("commits", [])
                if commits:
                    prompt += f"  Commits ({len(commits)}):\n"
                    for commit in commits[:5]:
                        msg = commit.get("message", "No message").split("\n")[0]
                        prompt += f"  - {msg}\n"

                prompt += "\n"

            # UI navigation context — use first ticket that has it
            for ticket in tickets_with_dev:
                dev_info = ticket["development_info"]
                repo_context = dev_info.get("repository_context")
                if not repo_context:
                    continue
                screen_guide = repo_context.get("screen_guide")
                testid_reference = repo_context.get("testid_reference")
                if screen_guide or testid_reference:
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "UI NAVIGATION CONTEXT\n"
                    prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    prompt += "\nThis app has stable testID identifiers. Use them in test steps instead of generic descriptions.\n"
                    if screen_guide:
                        guide_preview = screen_guide[:3000] + "\n...(truncated)" if len(screen_guide) > 3000 else screen_guide
                        prompt += f"\n**Screen Navigation Guide:**\n{guide_preview}\n"
                    if testid_reference:
                        ref_preview = testid_reference[:3000] + "\n...(truncated)" if len(testid_reference) > 3000 else testid_reference
                        prompt += f"\n**Available TestIDs:**\n{ref_preview}\n"
                    prompt += "\n⚠️ THE TESTID REFERENCE IS EXHAUSTIVE: every interactive element has a testID listed above. If a form field does NOT appear in the reference, it does not exist in this app — do NOT invent steps for it.\n"
                break

        # ── Final instructions ────────────────────────────────────────────────
        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        prompt += "INSTRUCTIONS\n"
        prompt += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        prompt += "Generate ONE unified test plan that covers all tickets above:\n"
        prompt += "- Treat all tickets as parts of a single combined feature\n"
        prompt += "- Merge test cases ONLY when the same user action covers multiple ACs — never drop an AC to reduce duplication\n"
        if ac_index:
            prompt += "- **REQUIRED:** Every AC ID listed under 'ACCEPTANCE CRITERIA TO COVER' must appear in at least one test case's `covers_acs` field — UNLESS the ID has been superseded by a newer ticket's AC (see 'AC CONFLICT RESOLUTION'). Superseded IDs are exempt from coverage and must be reported in the top-level `superseded_acs` array instead.\n"
            prompt += "- **REQUIRED:** If two ACs describe *different observable behaviours* — even within the same feature — they MUST have separate test cases. Examples: 'Add button adds PDF' and 'Preview opens overlay' are distinct user actions and need distinct tests; 'Save shows toast' and 'Save persists to file' verify different outcomes and need distinct tests. Do not collapse them into one case.\n"
            prompt += "- **REQUIRED:** `covers_acs` must contain only IDs that appear verbatim in the 'ACCEPTANCE CRITERIA TO COVER' list. Do not invent IDs (e.g. AC9 when only 8 ACs exist), do not renumber, do not guess. The ID you tag must match a test whose steps and expected result actually verify that AC's wording.\n"
        prompt += "- Prioritise integration tests that cover how the tickets interact\n"
        prompt += "- Use shared development context to understand the full scope of changes\n"
        prompt += "- **FILTER OUT build-time changes**: focus ONLY on runtime behaviour\n"
        prompt += "- **Ground every named UI element** in the PR diff, testID reference, or attached screenshots (see 'UI GROUNDING' below). If you can't, flag the test in `grounding_warnings` rather than inventing a label that may not ship.\n"

        if has_images:
            prompt += "\n**Note:** Screenshots or mockups from one or more tickets are attached. Use them for UI-specific test cases.\n"

        if any(_is_voice_ticket(t.get("summary"), t.get("description")) for t in tickets):
            prompt += VOICE_TESTING_GUIDANCE

        if any(_is_observability_ticket(t.get("summary"), t.get("description")) for t in tickets):
            prompt += OBSERVABILITY_TESTING_GUIDANCE

        prompt += UI_GROUNDING_GUIDANCE

        return prompt


class OllamaClient(LLMClient):
    """Ollama LLM client (local, free)."""

    def __init__(self):
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.llm_model

    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        images: list[tuple[str, str]] | None = None,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
        seed_regressions: list[dict] | None = None,
        bounce_history: list[dict] | None = None,
    ) -> TestPlan:
        """Generate test plan using Ollama."""
        # Note: Ollama doesn't support vision yet, so images are ignored
        if images:
            print("Warning: Ollama does not support image analysis. Images will be ignored.")

        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images), comments=comments, parent_info=parent_info, linked_info=linked_info, slack_messages=slack_messages, seed_regressions=seed_regressions, bounce_history=bounce_history
        )

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                )
                response.raise_for_status()

                data = response.json()
                response_text = data.get("response", "")

                # Parse JSON response
                try:
                    test_plan_data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    raise LLMError(
                        f"Failed to parse JSON response from Ollama: {e}",
                        error_type="service_unavailable"
                    ) from e

                test_plan_data = _scrub_test_plan_data(test_plan_data)

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                    superseded_acs=test_plan_data.get("superseded_acs") or None,
                    grounding_warnings=test_plan_data.get("grounding_warnings") or None,
                )

        except httpx.ConnectError as e:
            raise LLMError(
                f"Failed to connect to Ollama at {self.base_url}. Is Ollama running? Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Ollama request timed out after 300s. Try a smaller model or increase timeout. Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"Ollama returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e

    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan for multiple related tickets using Ollama."""
        if images:
            print("Warning: Ollama does not support image analysis. Images will be ignored.")

        prompt = self._build_multi_ticket_prompt(tickets, has_images=False)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                )
                response.raise_for_status()

                data = response.json()
                response_text = data.get("response", "")

                try:
                    test_plan_data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    raise LLMError(
                        f"Failed to parse JSON response from Ollama: {e}",
                        error_type="service_unavailable"
                    ) from e

                test_plan_data = _scrub_test_plan_data(test_plan_data)

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                    superseded_acs=test_plan_data.get("superseded_acs") or None,
                    grounding_warnings=test_plan_data.get("grounding_warnings") or None,
                )

        except httpx.ConnectError as e:
            raise LLMError(
                f"Failed to connect to Ollama at {self.base_url}. Is Ollama running? Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(
                f"Ollama request timed out after 300s. Error: {e}",
                error_type="service_unavailable"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LLMError(
                f"Ollama returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e

    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
        status: str | None = None,
        status_category: str | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket using Ollama."""
        return await self._ollama_bug_analysis([{
            "ticket_key": ticket_key,
            "summary": summary,
            "description": description,
            "development_info": development_info,
            "comments": comments,
            "linked_info": linked_info,
            "github_context": github_context,
            "status": status,
            "status_category": status_category,
        }])

    async def generate_multi_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        """Analyze multiple bug tickets using Ollama."""
        return await self._ollama_bug_analysis(tickets)

    async def summarize_ticket(self, summary: str, description: str | None) -> str:
        """Return a plain-language summary using Ollama."""
        desc_part = f"\n\nDescription:\n{description}" if description else ""
        prompt = (
            f"Summarize this Jira ticket in 2-3 plain sentences that a tester can quickly read. "
            f"Focus on what the feature/bug is, what it affects, and what a tester needs to know. "
            f"No jargon, no bullet points.\n\nTitle: {summary}{desc_part}"
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                result = response.json()
                return result.get("response", "").strip()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise LLMError(f"Cannot connect to Ollama at {self.base_url}", error_type="connection_failed") from e
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama returned error status {e.response.status_code}", error_type="service_unavailable") from e

    async def _ollama_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        prompt = self._build_bug_analysis_prompt(tickets)
        schema = {
            "type": "object",
            "properties": {
                "bug_summary": {"type": "string"},
                "root_cause": {"type": "string"},
                "fix_status": {"type": "string", "enum": ["not_fixed", "in_testing", "fixed"]},
                "fix_explanation": {"type": "string"},
                "regression_tests": {"type": "array", "items": {"type": "string"}},
                "similar_patterns": {"type": "array", "items": {"type": "string"}},
                "fix_complexity": {"type": "string"},
                "fix_effort_estimate": {"type": "string"},
                "fix_complexity_reasoning": {"type": "string"},
                "affected_flow": {"type": "array", "items": {"type": "string"}},
                "scope_of_impact": {"type": "array", "items": {"type": "string"}},
                "why_tests_miss": {"type": "string"},
                "is_regression": {"type": "boolean"},
                "regression_introduced_by": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "open_questions": {"type": "array", "items": {"type": "string"}},
                "suspect_symbols": {"type": "array", "items": {"type": "string"}},
            },
        }
        full_prompt = BUG_LENS_SYSTEM_PROMPT + "\n\n" + prompt + "\n\nReturn ONLY valid JSON matching this schema: " + json.dumps(schema)

        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model,
                        "prompt": full_prompt,
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.1},
                    },
                )
                response.raise_for_status()
                data = response.json()
                try:
                    parsed = json.loads(data.get("response", ""))
                except json.JSONDecodeError as e:
                    raise LLMError(f"Failed to parse JSON from Ollama: {e}", error_type="service_unavailable") from e

                return BugAnalysis(
                    bug_summary=parsed.get("bug_summary", ""),
                    root_cause=parsed.get("root_cause"),
                    fix_status=_normalize_fix_status(parsed.get("fix_status"), parsed.get("is_fixed")),
                    fix_explanation=parsed.get("fix_explanation"),
                    regression_tests=parsed.get("regression_tests", []),
                    similar_patterns=parsed.get("similar_patterns", []),
                    fix_complexity=parsed.get("fix_complexity"),
                    fix_effort_estimate=parsed.get("fix_effort_estimate"),
                    fix_complexity_reasoning=parsed.get("fix_complexity_reasoning"),
                    affected_flow=parsed.get("affected_flow"),
                    scope_of_impact=parsed.get("scope_of_impact"),
                    why_tests_miss=parsed.get("why_tests_miss"),
                    is_regression=parsed.get("is_regression"),
                    regression_introduced_by=parsed.get("regression_introduced_by"),
                    assumptions=parsed.get("assumptions"),
                    open_questions=parsed.get("open_questions"),
                    suspect_symbols=parsed.get("suspect_symbols") or None,
                )

        except httpx.ConnectError as e:
            raise LLMError(f"Failed to connect to Ollama at {self.base_url}: {e}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Ollama request timed out: {e}", error_type="service_unavailable") from e
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Ollama returned error {e.response.status_code}: {e.response.text}", error_type="service_unavailable") from e


TEST_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["critical", "high", "medium"]},
        "preconditions": {"type": "string", "description": "Assumed pre-state before step 1 (e.g. 'Logged in, Buyer toggle selected, Interest Rate modal open'). Only include setup that is shared/repeated across multiple tests. Omit if the test has unique setup steps."},
        "steps": {"type": "array", "items": {"type": "string"}},
        "expected": {"type": "string"},
        "test_data": {"type": "string"},
        "covers_acs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Acceptance-criteria IDs this case exercises, e.g. ['SK-2137-AC1', 'SK-2139-AC2']. Only used in multi-ticket mode when an 'ACCEPTANCE CRITERIA TO COVER' section is supplied; list every ID this case legitimately validates.",
        },
    },
    "required": ["title", "priority", "steps", "expected"],
}

SUBMIT_TEST_PLAN_TOOL = {
    "name": "submit_test_plan",
    "description": "Submit the structured test plan with all test cases.",
    "input_schema": {
        "type": "object",
        "properties": {
            "happy_path": {
                "type": "array",
                "items": TEST_CASE_SCHEMA,
            },
            "edge_cases": {
                "type": "array",
                "items": {
                    **TEST_CASE_SCHEMA,
                    "properties": {
                        **TEST_CASE_SCHEMA["properties"],
                        "category": {
                            "type": "string",
                            "enum": ["security", "boundary", "error_handling", "integration"],
                            "description": "security: auth/permissions/data exposure. boundary: min/max values, empty inputs, limits. error_handling: invalid input, disabled states, UI feedback on failure, persistence/reset behavior. integration: cross-service or multi-system interactions.",
                        },
                    },
                },
            },
            "integration_tests": {
                "type": "array",
                "items": TEST_CASE_SCHEMA,
            },
            "regression_checklist": {
                "type": "array",
                "items": {"type": "string"},
            },
            "superseded_acs": {
                "type": "array",
                "description": "Multi-ticket only. ACs from older tickets that were overridden by a newer ticket's AC about the same observable behaviour. Leave empty when there are no conflicts.",
                "items": {
                    "type": "object",
                    "properties": {
                        "loser_id": {
                            "type": "string",
                            "description": "The older ticket's AC ID (e.g. 'SK-2138-AC3'). Must match an ID from 'ACCEPTANCE CRITERIA TO COVER'.",
                        },
                        "winner_id": {
                            "type": "string",
                            "description": "The newer ticket's AC ID that overrides loser_id. Must match an ID from 'ACCEPTANCE CRITERIA TO COVER'.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One sentence: which observable behaviour both ACs disagree on, and how the newer one changes it.",
                        },
                    },
                    "required": ["loser_id", "winner_id", "reason"],
                },
            },
            "grounding_warnings": {
                "type": "array",
                "description": "UI elements you referenced in a test step but couldn't verify against the PR diff or testID reference (see 'UI GROUNDING' instructions). One entry per (ac_id, missing_element) pair. Leave empty when every named UI element is grounded.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ac_id": {
                            "type": "string",
                            "description": "The AC ID whose test you couldn't fully ground (e.g. 'SK-2138-AC5'). Must match an ID from 'ACCEPTANCE CRITERIA TO COVER'.",
                        },
                        "missing_element": {
                            "type": "string",
                            "description": "The UI element you couldn't find — e.g. 'Edit button on bulk-fill popover', 'HOA dues input field'.",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One sentence: where you looked (PR diff, testID reference) and why you couldn't confirm the element exists.",
                        },
                    },
                    "required": ["ac_id", "missing_element", "explanation"],
                },
            },
        },
        "required": ["happy_path", "edge_cases", "regression_checklist"],
    },
}


class ClaudeClient(LLMClient):
    """Claude API client (Anthropic, paid)."""

    def __init__(self):
        if not settings.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Please add it to your .env file to use Claude.",
                error_type="invalid"
            )

        self.api_key = settings.anthropic_api_key
        self.model = settings.llm_model or "claude-opus-4-6"

    async def generate_test_plan(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        testing_context: dict,
        development_info: dict | None = None,
        images: list[tuple[str, str]] | None = None,
        comments: list[dict] | None = None,
        parent_info: dict | None = None,
        linked_info: dict | None = None,
        slack_messages: list[dict] | None = None,
        seed_regressions: list[dict] | None = None,
        bounce_history: list[dict] | None = None,
    ) -> TestPlan:
        """Generate test plan using Claude API with optional image support."""
        prompt = self._build_prompt(
            ticket_key, summary, description, testing_context, development_info, has_images=bool(images), comments=comments, parent_info=parent_info, linked_info=linked_info, slack_messages=slack_messages, seed_regressions=seed_regressions, bounce_history=bounce_history
        )

        # Build message content (text + images if provided)
        content = []

        # Add images first if available
        if images:
            for base64_data, media_type in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64_data,
                    },
                })

        # Add text prompt
        content.append({
            "type": "text",
            "text": prompt,
        })

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 8192,
                        "system": [
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": content}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_TEST_PLAN_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_test_plan"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError(
                        "Claude did not return a tool_use block. Unexpected response format.",
                        error_type="service_unavailable",
                    )
                test_plan_data = _scrub_test_plan_data(tool_block["input"])

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                    superseded_acs=test_plan_data.get("superseded_acs") or None,
                    grounding_warnings=test_plan_data.get("grounding_warnings") or None,
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Try to parse error message
                error_msg = ""
                try:
                    error_data = e.response.json()
                    error_msg = error_data.get("error", {}).get("message", "")
                except Exception:
                    pass

                if "invalid" in error_msg.lower():
                    raise LLMError(
                        "Anthropic API key is invalid. Please check your ANTHROPIC_API_KEY in .env or generate a new key at https://console.anthropic.com/settings/keys",
                        error_type="invalid"
                    ) from e
                else:
                    raise LLMError(
                        "Anthropic API authentication failed. Your API key may be expired or revoked. Get a new key at https://console.anthropic.com/settings/keys",
                        error_type="expired"
                    ) from e
            elif e.response.status_code == 429:
                raise LLMError(
                    "Anthropic API rate limit exceeded. Please wait and try again.",
                    error_type="rate_limited"
                ) from e
            raise LLMError(
                f"Claude API returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}", error_type="service_unavailable") from e

    async def generate_multi_ticket_test_plan(
        self,
        tickets: list[dict],
        images: list[tuple[str, str]] | None = None,
    ) -> TestPlan:
        """Generate a unified test plan for multiple related tickets using Claude API."""
        prompt = self._build_multi_ticket_prompt(tickets, has_images=bool(images))

        content = []

        if images:
            for base64_data, media_type in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64_data,
                    },
                })

        content.append({"type": "text", "text": prompt})

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 8192,
                        "system": [
                            {
                                "type": "text",
                                "text": SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": content}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_TEST_PLAN_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_test_plan"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError(
                        "Claude did not return a tool_use block. Unexpected response format.",
                        error_type="service_unavailable",
                    )
                test_plan_data = _scrub_test_plan_data(tool_block["input"])

                return TestPlan(
                    happy_path=test_plan_data.get("happy_path", []),
                    edge_cases=test_plan_data.get("edge_cases", []),
                    regression_checklist=test_plan_data.get("regression_checklist", []),
                    integration_tests=test_plan_data.get("integration_tests", []),
                    superseded_acs=test_plan_data.get("superseded_acs") or None,
                    grounding_warnings=test_plan_data.get("grounding_warnings") or None,
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                error_msg = ""
                try:
                    error_data = e.response.json()
                    error_msg = error_data.get("error", {}).get("message", "")
                except Exception:
                    pass
                if "invalid" in error_msg.lower():
                    raise LLMError(
                        "Anthropic API key is invalid.",
                        error_type="invalid"
                    ) from e
                else:
                    raise LLMError(
                        "Anthropic API authentication failed.",
                        error_type="expired"
                    ) from e
            elif e.response.status_code == 429:
                raise LLMError(
                    "Anthropic API rate limit exceeded. Please wait and try again.",
                    error_type="rate_limited"
                ) from e
            raise LLMError(
                f"Claude API returned error status {e.response.status_code}: {e.response.text}",
                error_type="service_unavailable"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}", error_type="service_unavailable") from e

    async def generate_bug_analysis(
        self,
        ticket_key: str,
        summary: str,
        description: str | None,
        development_info: dict | None = None,
        comments: list[dict] | None = None,
        linked_info: dict | None = None,
        github_context: list[dict] | None = None,
        status: str | None = None,
        status_category: str | None = None,
    ) -> BugAnalysis:
        """Analyze a bug ticket using Claude API."""
        return await self._claude_bug_analysis([{
            "ticket_key": ticket_key,
            "summary": summary,
            "description": description,
            "development_info": development_info,
            "comments": comments,
            "linked_info": linked_info,
            "github_context": github_context,
            "status": status,
            "status_category": status_category,
        }])

    async def generate_multi_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        """Analyze multiple bug tickets using Claude API."""
        return await self._claude_bug_analysis(tickets)

    async def summarize_ticket(self, summary: str, description: str | None) -> str:
        """Return a plain-language summary using Claude API."""
        desc_part = f"\n\nDescription:\n{description}" if description else ""
        prompt = (
            f"Summarize this Jira ticket in 2-3 plain sentences that a tester can quickly read. "
            f"Focus on what the feature/bug is, what it affects, and what a tester needs to know. "
            f"No jargon, no bullet points. Reply with only the summary text.\n\nTitle: {summary}{desc_part}"
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 256,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                    },
                )
                response.raise_for_status()
                result = response.json()
                return result["content"][0]["text"].strip()
        except httpx.HTTPStatusError as e:
            raise LLMError(f"Claude API returned error status {e.response.status_code}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError("Claude API request timed out", error_type="service_unavailable") from e

    async def _claude_bug_analysis(self, tickets: list[dict]) -> BugAnalysis:
        prompt = self._build_bug_analysis_prompt(tickets)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "anthropic-version": "2023-06-01",
                        "x-api-key": self.api_key,
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 4096,
                        "system": [
                            {
                                "type": "text",
                                "text": BUG_LENS_SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "tools": [SUBMIT_BUG_ANALYSIS_TOOL],
                        "tool_choice": {"type": "tool", "name": "submit_bug_analysis"},
                    },
                )
                response.raise_for_status()

                data = response.json()
                tool_block = next(
                    (b for b in data["content"] if b.get("type") == "tool_use"),
                    None,
                )
                if tool_block is None:
                    raise LLMError("Claude did not return a tool_use block.", error_type="service_unavailable")

                parsed = tool_block["input"]
                return BugAnalysis(
                    bug_summary=parsed.get("bug_summary", ""),
                    root_cause=parsed.get("root_cause"),
                    fix_status=_normalize_fix_status(parsed.get("fix_status"), parsed.get("is_fixed")),
                    fix_explanation=parsed.get("fix_explanation"),
                    regression_tests=parsed.get("regression_tests", []),
                    similar_patterns=parsed.get("similar_patterns", []),
                    fix_complexity=parsed.get("fix_complexity"),
                    fix_effort_estimate=parsed.get("fix_effort_estimate"),
                    fix_complexity_reasoning=parsed.get("fix_complexity_reasoning"),
                    affected_flow=parsed.get("affected_flow"),
                    scope_of_impact=parsed.get("scope_of_impact"),
                    why_tests_miss=parsed.get("why_tests_miss"),
                    is_regression=parsed.get("is_regression"),
                    regression_introduced_by=parsed.get("regression_introduced_by"),
                    assumptions=parsed.get("assumptions"),
                    open_questions=parsed.get("open_questions"),
                    suspect_symbols=parsed.get("suspect_symbols") or None,
                )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise LLMError("Anthropic API key is invalid or expired.", error_type="invalid") from e
            elif e.response.status_code == 429:
                raise LLMError("Anthropic API rate limit exceeded.", error_type="rate_limited") from e
            raise LLMError(f"Claude API error {e.response.status_code}: {e.response.text}", error_type="service_unavailable") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"Claude API request timed out: {e}", error_type="service_unavailable") from e


BUG_LENS_SYSTEM_PROMPT = """You are a senior software engineer performing a structured bug post-mortem. Your job is to analyze a Jira bug ticket — and any associated code changes — and produce a clear, grounded analysis.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT YOU MUST DO
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **bug_summary** — Explain the bug in plain English. What was the user-facing symptom? What was broken?

2. **root_cause** — If code diffs are available, identify the exact cause in the code. Reference file names and what the faulty logic was. If no diff is available, derive the likely cause from the ticket description and comments.

3. **fix_status** — Tri-state describing how far the fix has progressed. **The Jira workflow status is the source of truth — PR state alone never overrides the column the ticket sits in.** Use the Jira status (and its category: "new" = To Do, "indeterminate" = In Progress, "done" = Done) together with PR state:
   - "fixed": Jira status category is "done" (e.g. "Done", "Closed", "Resolved"). Typically a merged PR also exists. The bug has been fixed AND validated/released.
   - "in_testing": the code change is in AND the ticket has moved past dev work. Use this ONLY when the Jira status name explicitly implies QA/testing/code-review/ready-for-release (e.g. "In Testing", "QA", "Code Review", "Ready for QA", "Ready for Release", "Awaiting Verification"). A merged PR alone does NOT qualify — the workflow column must reflect that dev is done.
   - "not_fixed": Jira status category is "new" (To Do) OR "indeterminate" with a generic in-progress status name (e.g. "In Progress", "Open"). This applies even when merged PRs are attached — those PRs are likely diagnostic logging, partial work, refactors, or PRs that merely mention the ticket key. If QA hasn't moved the column out of To Do/In Progress, the bug is not yet in testing.
   **Heuristic for ambiguous cases:** when the Jira status is "To Do" but merged PRs exist, scan the PR titles and diffs — if the PRs are clearly preliminary work (logging, instrumentation, refactors) rather than a fix matching the bug's root cause, choose "not_fixed" and surface the disconnect in `assumptions` or `open_questions`.

4. **fix_explanation** — If fix_status is "fixed" or "in_testing", explain what the code change did to resolve the bug. Reference specific files and the nature of the change. When in_testing, frame it as the proposed/landed fix awaiting validation. If fix_status is "not_fixed", set to null.

5. **regression_tests** — List concrete, specific test cases a QA engineer can run to verify this exact bug does not recur. Each item must be a complete, actionable test description (not a category). Be specific: include the scenario, the input or action, and the expected outcome.

6. **similar_patterns** — List classes of related bugs that could exist elsewhere in the codebase based on the same root cause. These help the team proactively find similar issues.

7. **affected_flow** — A numbered list of steps tracing the end-to-end path from user action to the bug. Format each step as a short sentence, e.g. "1. User clicks Submit → 2. Frontend calls POST /api/calculate → 3. Handler calls FeeService.compute() → 4. compute() divides by zero when payor is null". If you cannot determine the flow from the available evidence, set to null.

8. **scope_of_impact** — Other callers or features affected by the same broken code. When code context is available (diffs, fetched files, linked code), each entry MUST name a specific file, component, or symbol (e.g. "apps/expo/src/screens/Folders.tsx uses the same BrandedHeader"). Screen names or feature categories alone ("Folders screen header") are only acceptable when no code context is available. Set to null if no other callers are identifiable from the evidence.

9. **why_tests_miss** — A single plain-English explanation of why the existing test suite did not catch this bug (e.g. mocking bypassed the broken layer, only happy-path covered, no integration test for this flow). If you cannot determine this from the evidence, set to null.

10. **is_regression** — Set to true if the bug was previously working and a specific code change broke it. Set to false if the feature was never functional. Set to null if you cannot determine this from the available evidence.

11. **regression_introduced_by** — If is_regression is true, identify the PR title, PR number, commit SHA, or branch name that introduced the breakage. Set to null if is_regression is false or unknown.

12. **fix_complexity** — Only when fix_status is "not_fixed" (work hasn't started or is still in progress). Classify the expected fix effort as one of:
   - "trivial": a one-liner or config change, no risk of side effects
   - "moderate": a focused code change in 1–2 files, straightforward logic fix
   - "complex": touches multiple files or services, requires careful testing
   - "architectural": requires design changes, schema migrations, or cross-team coordination
   Set to null if fix_status is "in_testing" or "fixed".

13. **fix_effort_estimate** — Only when fix_status is "not_fixed". A concise time range for a competent engineer who knows the codebase (e.g. "1–2 hours", "half a day", "2–3 days", "1+ week"). **If the scope is genuinely ambiguous (e.g. the ticket could mean a narrow UI tweak or a broader data-model change), give a branched estimate instead of averaging**, e.g. "2h if scoped to the existing single-color header / 4–5h if supporting per-rep colors end-to-end". Do not pick a single midpoint when the scope itself is unclear — the ambiguity belongs in the estimate. **Coupling with open_questions:** if `open_questions` contains ANY question about scope, feature breadth, or which alternative is intended, then `fix_effort_estimate` MUST be a branched estimate whose branches correspond to those alternatives. A single unconditional range is only valid when `open_questions` contains no scope-level questions. Widening a single range (e.g. "2–4 hours") is NOT an acceptable substitute for branching — it hides the ambiguity rather than exposing it. Set to null if fix_status is "in_testing" or "fixed".

14. **fix_complexity_reasoning** — Only when fix_status is "not_fixed". 1–2 sentences explaining why you assigned that complexity level. Reference specific files, services, or constraints. Set to null if fix_status is "in_testing" or "fixed".

15. **assumptions** — Inferences you made that are NOT directly grounded in the evidence, but that your analysis depends on. Example: "Assumed each Title Rep has their own assigned color (ticket says 'rep's assigned color' but code context only shows a single hardcoded value)." List every non-trivial leap so a reviewer can verify them. Set to null only if your analysis makes no such inferences.

16. **open_questions** — Interpretation ambiguities a human should resolve before committing to the estimate or fix. Phrase each as a question. Example: "Is the bug scoped to making the existing yellow header's contrast work, or does it include supporting arbitrary per-rep colors?" Set to null only if the scope is fully unambiguous from the evidence.

17. **suspect_symbols** — 1–3 code symbol names (component, function, class, or distinctive identifier) most likely implicated in the bug. These will be used to run a deterministic code search in the repo to produce a "Code Evidence" section, so pick names that are (a) likely to exist verbatim in the codebase and (b) specific enough to return meaningful hits. Good picks: `BrandedHeader`, `calculateAgentFee`, `useTitleRepColor`. Bad picks: generic words like `button`, `color`, `screen` (too many false matches). Prefer PascalCase component names and snake_case/camelCase function names over feature descriptions. Return an empty list (not null) if you cannot identify any specific symbols from the evidence — do NOT guess.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GROUNDING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Base your analysis only on what is in the ticket, PR description, and code diffs provided.
- Do NOT invent root causes not supported by the evidence.
- Do NOT add regression tests for unrelated features.
- If a diff is not available, say so in root_cause and work from the ticket description only.
- Keep all text concise and technical — this is read by engineers and QA, not end users.
- For fix_complexity, fix_effort_estimate, and fix_complexity_reasoning: set all three to null when fix_status is "in_testing" or "fixed" (the dev work is no longer the bottleneck — there is nothing left to estimate).
- Surface ambiguity explicitly in assumptions and open_questions rather than resolving it silently. A confident-looking analysis that papers over interpretation gaps is worse than one that names them."""


SUBMIT_BUG_ANALYSIS_TOOL = {
    "name": "submit_bug_analysis",
    "description": "Submit the structured bug analysis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bug_summary": {
                "type": "string",
                "description": "Plain-English explanation of what the bug is and its user-facing impact.",
            },
            "root_cause": {
                "type": ["string", "null"],
                "description": "The technical root cause in the code. Reference file names if diffs are available.",
            },
            "fix_status": {
                "type": "string",
                "enum": ["not_fixed", "in_testing", "fixed"],
                "description": "Tri-state fix progress. Jira workflow status is the source of truth. 'fixed' = Jira Done category. 'in_testing' = Jira status name explicitly says QA/Testing/Code Review/Ready for Release. 'not_fixed' = Jira To Do or generic In Progress, even if merged PRs exist (those may be logging/refactor/partial work).",
            },
            "fix_explanation": {
                "type": ["string", "null"],
                "description": "What the fix did. Provide for 'fixed' or 'in_testing'. Null when fix_status is 'not_fixed'.",
            },
            "regression_tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete, specific test cases to prevent this bug from recurring.",
            },
            "similar_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Classes of similar bugs to proactively look for in the codebase.",
            },
            "fix_complexity": {
                "type": ["string", "null"],
                "enum": ["trivial", "moderate", "complex", "architectural", None],
                "description": "Estimated fix complexity. One of: trivial, moderate, complex, architectural. Null when fix_status is 'in_testing' or 'fixed'.",
            },
            "fix_effort_estimate": {
                "type": ["string", "null"],
                "description": "Estimated time to fix for a competent engineer (e.g. '2–4 hours', '1–2 days'). Null when fix_status is 'in_testing' or 'fixed'.",
            },
            "fix_complexity_reasoning": {
                "type": ["string", "null"],
                "description": "1–2 sentences explaining the complexity rating. Reference files, services, or constraints. Null when fix_status is 'in_testing' or 'fixed'.",
            },
            "affected_flow": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Numbered steps tracing the end-to-end path from user action to the bug. Each step is a short sentence. Null if flow cannot be determined.",
            },
            "scope_of_impact": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Other features, endpoints, or callers that invoke the same broken code and are also affected. Null if none identifiable.",
            },
            "why_tests_miss": {
                "type": ["string", "null"],
                "description": "Plain-English explanation of why existing tests did not catch this bug. Null if cannot be determined.",
            },
            "is_regression": {
                "type": ["boolean", "null"],
                "description": "True if the bug was previously working and a code change broke it. False if never functional. Null if unknown.",
            },
            "regression_introduced_by": {
                "type": ["string", "null"],
                "description": "PR title, PR number, commit SHA, or branch name that introduced the regression. Null if is_regression is false or unknown.",
            },
            "assumptions": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Non-trivial inferences the analysis depends on but that are NOT directly grounded in the ticket, comments, or code context. Each item names the inference so a reviewer can verify it. Null only if the analysis makes no such inferences.",
            },
            "open_questions": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Interpretation ambiguities a human should resolve before committing to the estimate or fix. Each item is phrased as a question. Null only if scope is fully unambiguous.",
            },
            "suspect_symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1–3 specific code symbol names (component/function/class identifiers) likely implicated in the bug, for use in a deterministic code search. Return an empty list if no specific symbols are identifiable from the evidence — do not guess.",
            },
        },
        "required": ["bug_summary", "root_cause", "fix_status", "fix_explanation", "regression_tests", "similar_patterns", "fix_complexity", "fix_effort_estimate", "fix_complexity_reasoning", "affected_flow", "scope_of_impact", "why_tests_miss", "is_regression", "regression_introduced_by", "assumptions", "open_questions", "suspect_symbols"],
    },
}


def get_llm_client() -> LLMClient:
    """
    Factory function to get the appropriate LLM client based on configuration.

    Returns:
        LLMClient: The configured LLM client (Ollama or Claude)

    Raises:
        LLMError: If provider is unsupported or configuration is invalid
    """
    provider = settings.llm_provider.lower()

    if provider == "ollama":
        return OllamaClient()
    elif provider == "claude":
        return ClaudeClient()
    else:
        raise LLMError(
            f"Unsupported LLM provider: {provider}. Use 'ollama' or 'claude'.",
            error_type="invalid"
        )
