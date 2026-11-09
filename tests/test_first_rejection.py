"""Rule Q, first rejecting check (src/app/first_rejection.py).

Built from two rows of a real plan (SK-2825, plan 673), sanitized: the repo,
files and messages are ``acme`` placeholders with the same execution order.

* edge_cases:3 — a table of outer-envelope mutations all expected to fail with
  the field-comparison message; the method=PUT row is really rejected first by
  the HTTP-method allow-list, before decryption and comparison run.
* edge_cases:5 — IP-literal rows all expected as "is not a public address";
  ``[::1]`` and ``[::ffff:10.0.0.1]`` are really rejected first by URL encoding
  as "not a valid absolute URL", so the address policy never runs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app import first_rejection as fr
from src.app import plan_recheck as pr
from src.app.source_facts import SourceFile, SourceIndex

REPO = "acme/relay-functions"

# The real entry point and its checks, in execution order.
HANDLER = "relay/handler.js"
METHOD = "relay/method-policy.js"
ENCODE = "relay/url-encoding.js"
BIND = "relay/envelope-binding.js"
DEST = "relay/destination-policy.js"
SOURCES = {
    HANDLER: "\n".join([
        "async function deliver(envelope) {",                       # 1
        "  assertAllowedMethod(envelope.method);",                  # 2
        "  const url = encodeDestination(envelope.targetUrl);",     # 3
        "  const sealed = decrypt(envelope);",                      # 4
        "  bindEnvelope(envelope, sealed);",                        # 5
        "  await checkDestination({ url, method: envelope.method });",  # 6
        "  return send(url, sealed.payload);",                      # 7
        "}",
    ]),
    METHOD: "\n".join([
        "const ALLOWED = new Set(['POST']);",
        "function assertAllowedMethod(method) {",
        "  if (!ALLOWED.has(method)) throw new Error(`HTTP method '${method}' is not allowed for relay delivery`);",
        "}",
    ]),
    ENCODE: "\n".join([
        "function encodeDestination(raw) {",
        "  let u; try { u = new URL(raw); } catch { u = null; }",
        "  if (!u || !u.hostname || u.hostname.startsWith('[')) throw new Error('Destination URL is not a valid absolute URL');",
        "  return u;",
        "}",
    ]),
    BIND: "\n".join([
        "function bindEnvelope(outer, sealed) {",
        "  for (const f of ['targetUrl', 'method', 'payloadType', 'authentication']) {",
        "    if (f in outer && JSON.stringify(outer[f]) !== JSON.stringify(sealed[f]))",
        "      throw new Error(`Outer envelope field '${f}' does not match the encrypted payload`);",
        "  }",
        "}",
    ]),
    DEST: "\n".join([
        "async function checkDestination({ url }) {",
        "  if (isBlockedLiteral(url.hostname))",
        "    throw new Error(`Destination host '${url.hostname}' is not a public address`);",
        "}",
    ]),
}

OUTCOME = "Every row is rejected and no outbound request is sent."


def _index():
    idx = SourceIndex()
    for path, text in SOURCES.items():
        idx.files[(REPO, path)] = SourceFile(REPO, path, "h" * 40, text)
    idx.refs[REPO] = "h" * 40
    idx.trees[REPO] = list(SOURCES)
    return idx


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": [],
            "security_negative_tests": []}
    return SimpleNamespace(**{**base, **sections})


def _outer_fields_case(**kw):
    """SK-2825 edge_cases:3, sanitized."""
    return {
        "title": "Outer copies that differ from the sealed values are rejected",
        "steps": ["Build a bound envelope and overwrite its outer targetUrl; invoke deliver().",
                  "Repeat with the outer method set to PUT.",
                  "Repeat with the outer payloadType set to LEGACY_XML.",
                  "Assert zero outbound requests across all rows."],
        "expected": ("Rows 1-3 are rejected with \"Outer envelope field '<field>' does not match the encrypted "
                     f"payload\" ({BIND}:4). No outbound request in any row."),
        "test_data": ("| Mutation | Value |\n|---|---|\n| outer targetUrl | https://attacker.invalid/collect |\n"
                      "| outer method | PUT |\n| outer payloadType | LEGACY_XML |"),
        "how_to_verify": "Jest output for the binding tests.",
        "expected_verified": True, "expected_source": f"{BIND}:3-4", "expected_outcome": OUTCOME, **kw}


def _ip_literals_case(**kw):
    """SK-2825 edge_cases:5, sanitized (the IP-literal rows)."""
    return {
        "title": "Blocked IP literals never get a request, even when the destination is sealed",
        "steps": ["For each row, build a bound envelope whose sealed targetUrl is that URL.",
                  "Invoke deliver() and record the rejection message.", "Assert zero outbound requests."],
        "expected": ("Every row is rejected before any request: IP literals in blocked ranges (127.0.0.1, [::1], "
                     "[::ffff:10.0.0.1]) → \"...is not a public address\". No request in any row."),
        "test_data": ("| Case | URL |\n|---|---|\n| loopback literal | https://127.0.0.1/hook |\n"
                      "| IPv6 loopback | https://[::1]/hook |\n| IPv4-mapped private | https://[::ffff:10.0.0.1]/hook |"),
        "how_to_verify": "Jest output for the destination-policy table.",
        "expected_verified": True, "expected_source": f"{DEST}:2-3", "expected_outcome": OUTCOME, **kw}


def _trace(inp, check, message, via=()):
    hops = [f"{HANDLER}:{n}" for n in via]
    return {"input": inp, "traced_path": " → ".join([f"{HANDLER}:1 deliver()", *hops, check]),
            "first_rejecting_check": check, "message": message}


# --- SK-2825 edge_cases:3 ----------------------------------------------------

def test_sk2825_one_message_for_a_whole_table_is_downgraded_to_the_outcome():
    plan = _plan(edge_cases=[_outer_fields_case()])
    report: dict = {}
    assert fr.check_pinned_messages(plan, report, _index()) == 1
    case = plan.edge_cases[0]
    assert case["expected"].startswith("Exact message unverified (rule Q, first rejecting check):")
    assert OUTCOME in case["expected"] and "record the exact message" in case["expected"]
    assert "Original expected: Rows 1-3" in case["expected"], "the prediction stays visible"
    assert case["expected_verified"] is False
    f = report["findings"][0]
    assert f["rule"] == "Q" and f["action"] == "downgraded" and "a table of 3 inputs" in f["reason"]
    assert case["grounding_downgrades"][0]["claim"].startswith("rule Q:")


def test_sk2825_per_row_traces_that_match_expected_keep_the_exact_text():
    case = _outer_fields_case(
        expected=("Rows 1 and 3 are rejected with \"Outer envelope field '<field>' does not match the encrypted "
                  f"payload\" ({BIND}:4); row 2 (PUT) is rejected first by the method allow-list with \"HTTP method "
                  f"'PUT' is not allowed for relay delivery\" ({METHOD}:3). No outbound request in any row."),
        rejection_traces=[
            _trace("outer targetUrl", f"{BIND}:4", "Outer envelope field 'targetUrl' does not match the encrypted payload", via=(2, 3, 5)),
            _trace("outer method PUT", f"{METHOD}:3", "HTTP method 'PUT' is not allowed for relay delivery", via=(2,)),
            _trace("outer payloadType", f"{BIND}:4", "Outer envelope field 'payloadType' does not match the encrypted payload", via=(2, 3, 5)),
        ])
    plan = _plan(edge_cases=[case])
    report: dict = {}
    assert fr.check_pinned_messages(plan, report, _index()) == 0
    assert plan.edge_cases[0]["expected"] == case["expected"]
    assert plan.edge_cases[0]["pinned_message_check"]["verdict"] == "traced"
    assert not report.get("findings")


def test_sk2825_a_trace_citing_a_check_that_does_not_produce_the_message_is_downgraded():
    """The PUT row 'traced' to the method allow-list line but still quoting the comparison message."""
    case = _outer_fields_case(rejection_traces=[
        _trace("outer targetUrl", f"{BIND}:4", "Outer envelope field 'targetUrl' does not match the encrypted payload", via=(2, 3, 5)),
        _trace("outer method PUT", f"{METHOD}:3", "Outer envelope field 'method' does not match the encrypted payload", via=(2,)),
        _trace("outer payloadType", f"{BIND}:4", "Outer envelope field 'payloadType' does not match the encrypted payload", via=(2, 3, 5)),
    ])
    plan = _plan(edge_cases=[case])
    report: dict = {}
    assert fr.check_pinned_messages(plan, report, _index()) == 1
    assert f"{METHOD}:3 does not produce" in report["findings"][0]["reason"]


# --- SK-2825 edge_cases:5 ----------------------------------------------------

def test_sk2825_rows_rejected_first_by_url_encoding_are_named_in_the_downgrade():
    case = _ip_literals_case(rejection_traces=[
        _trace("127.0.0.1", f"{DEST}:3", "Destination host '127.0.0.1' is not a public address", via=(2, 3, 5, 6)),
        _trace("[::1]", f"{ENCODE}:3", "Destination URL is not a valid absolute URL", via=(2, 3)),
        _trace("[::ffff:10.0.0.1]", f"{ENCODE}:3", "Destination URL is not a valid absolute URL", via=(2, 3)),
    ])
    plan = _plan(edge_cases=[case])
    report: dict = {}
    assert fr.check_pinned_messages(plan, report, _index()) == 1
    reason = report["findings"][0]["reason"]
    assert "[::1] is rejected first by" in reason and "not a valid absolute URL" in reason
    assert "Expected does not say" in reason
    assert plan.edge_cases[0]["expected"].count("Exact message unverified") == 1


def test_sk2825_ip_rows_with_the_right_message_per_row_pass():
    case = _ip_literals_case(
        expected=("Every row is rejected before any request: 127.0.0.1 → \"Destination host '127.0.0.1' is not a "
                  "public address\"; [::1] and [::ffff:10.0.0.1] → \"Destination URL is not a valid absolute URL\" "
                  "(URL encoding rejects them before the address policy). No request in any row."),
        rejection_traces=[
            _trace("127.0.0.1", f"{DEST}:3", "Destination host '127.0.0.1' is not a public address", via=(2, 3, 5, 6)),
            _trace("[::1]", f"{ENCODE}:3", "Destination URL is not a valid absolute URL", via=(2, 3)),
            _trace("[::ffff:10.0.0.1]", f"{ENCODE}:3", "Destination URL is not a valid absolute URL", via=(2, 3)),
        ])
    plan = _plan(edge_cases=[case])
    assert fr.check_pinned_messages(plan, {}, _index()) == 0


# --- general behaviour -------------------------------------------------------

def test_a_single_input_with_a_one_hop_citation_is_not_a_trace():
    case = {"title": "PUT is refused", "steps": ["Send PUT"], "how_to_verify": "Response body",
            "expected": "The request is rejected with \"HTTP method 'PUT' is not allowed for relay delivery\".",
            "traced_path": f"{METHOD}:3"}
    plan = _plan(edge_cases=[case])
    assert fr.check_pinned_messages(plan, {}, _index()) == 1
    # No expected_outcome: the sentence is kept as the prediction behind the marker.
    assert "Predicted: The request is rejected with" in plan.edge_cases[0]["expected"]


def test_a_single_input_traced_from_the_entry_point_keeps_its_text():
    case = {"title": "PUT is refused", "steps": ["Send PUT"], "how_to_verify": "Response body",
            "expected": "The request is rejected with \"HTTP method 'PUT' is not allowed for relay delivery\".",
            "traced_path": f"{HANDLER}:2 assertAllowedMethod → {METHOD}:3 throws 'HTTP method 'PUT' is not "
                           f"allowed for relay delivery'"}
    plan = _plan(edge_cases=[case])
    assert fr.check_pinned_messages(plan, {}, _index()) == 0


def test_status_codes_and_error_classes_are_pins_too():
    assert [p["kind"] for p in fr.pinned("The API returns 403 and throws ForbiddenError.")] == ["status", "error_class"]
    plan = _plan(security_negative_tests=[{"title": "No token", "steps": ["Call without a token"],
                                           "expected": "The API returns 401.", "how_to_verify": "status"}])
    report: dict = {}
    assert fr.check_pinned_messages(plan, report) == 1
    assert report["findings"][0]["section"] == "security_negative_tests"


def test_ui_labels_and_success_text_are_not_pins():
    assert fr.pinned("Tap \"Save changes\"; the toast \"Your changes were saved\" appears.") == []
    assert fr.pinned("The banner reads 'Upload complete for all files'.") == []


def test_unread_source_never_flags_a_traced_case():
    case = _outer_fields_case(
        expected=f"Row 2 is rejected with \"HTTP method 'PUT' is not allowed for relay delivery\" ({METHOD}:3).",
        test_data="", steps=["Send it with method PUT"],
        rejection_traces=[_trace("PUT", "elsewhere/unread.js:9", "HTTP method 'PUT' is not allowed for relay delivery", via=(2,))])
    assert fr.check_pinned_messages(_plan(edge_cases=[case]), {}, _index()) == 0


def test_runs_in_recheck_before_save_and_is_idempotent():
    plan = _plan(edge_cases=[_outer_fields_case()])
    report: dict = {}
    pr.recheck_before_save(plan, report, index=_index())
    first = plan.edge_cases[0]["expected"]
    assert any(f["rule"] == "Q" for f in report["findings"])
    pr.recheck_before_save(plan, {}, index=_index())
    assert plan.edge_cases[0]["expected"] == first


# --- the trace critic checks the first rejecting check ----------------------

class _FakeLLM:
    model = "m"

    def __init__(self, turns):
        self.turns, self.bodies = list(turns), []

    def _budget(self, *a, **k):
        return 1000

    def _effort_kwargs(self, *_a):
        return {}

    async def _post_messages(self, body, **_kw):
        self.bodies.append(body)
        return self.turns.pop(0)


class _ReadGH:
    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        return {p: SOURCES.get(p, "") for p in paths}


DEV = [{"pull_requests": [{"repository": REPO, "number": 7, "head_sha": "h" * 40,
                           "files_changed": [{"filename": BIND, "patch": "+ bindEnvelope"}]}]}]


def _report(verdict):
    return {"content": [{"type": "tool_use", "id": "r", "name": "report_recheck",
                         "input": {"verdicts": [verdict]}}]}


def _read(path):
    return {"content": [{"type": "tool_use", "id": "a", "name": "read_file", "input": {"repo": REPO, "path": path}}]}


def test_pinned_cases_are_traced_first_and_rows_are_passed_to_the_critic():
    filler = [{"title": f"Offline {n}", "expected": "An error alert appears.", "steps": ["Go offline"]}
              for n in range(12)]
    plan = _plan(happy_path=filler, edge_cases=[_outer_fields_case()])
    items = pr.collect_trace_items(plan)
    assert items[0]["id"] == "edge_cases:0:q" and len(items[0]["rows"]) == 3
    msg = pr.build_trace_message(items[:1], DEV[0]["pull_requests"], {})
    assert "Pinned error text (check kind 3)" in msg and "Trace each of these 3 rows separately" in msg
    assert "FIRST REJECTING CHECK" in pr.TRACE_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_the_critic_contradicting_the_put_row_downgrades_to_the_outcome():
    verdict = {"id": "edge_cases:0:q", "verdict": "contradicted",
               "traced_path": f"{HANDLER}:2 assertAllowedMethod → {METHOD}:3",
               "actual": f"row 2 (PUT) is rejected first at {METHOD}:3 with \"HTTP method 'PUT' is not allowed for "
                         f"relay delivery\"; bindEnvelope never runs",
               "reason": "method allow-list runs before decryption"}
    plan = _plan(edge_cases=[_outer_fields_case()])
    report: dict = {}
    await pr.run_trace_critic(_FakeLLM([_read(METHOD), _report(verdict)]), _ReadGH(), plan, DEV, report)
    case = plan.edge_cases[0]
    assert case["expected"].startswith("Contradicted by the code (rule Q): row 2 (PUT)")
    assert OUTCOME in case["expected"] and case["expected_verified"] is False
    # The save-time check does not stack a second marker.
    assert fr.check_pinned_messages(plan, {}, _index()) == 0


@pytest.mark.asyncio
async def test_rows_the_critic_traced_become_the_cases_rejection_traces():
    case = _outer_fields_case(
        expected=("Rows 1 and 3: \"Outer envelope field '<field>' does not match the encrypted payload\"; row 2 (PUT): "
                  "\"HTTP method 'PUT' is not allowed for relay delivery\". No outbound request."))
    rows = [_trace("outer targetUrl", f"{BIND}:4", "Outer envelope field 'targetUrl' does not match the encrypted payload", via=(2, 3, 5)),
            _trace("outer method PUT", f"{METHOD}:3", "HTTP method 'PUT' is not allowed for relay delivery", via=(2,)),
            _trace("outer payloadType", f"{BIND}:4", "Outer envelope field 'payloadType' does not match the encrypted payload", via=(2, 3, 5))]
    verdict = {"id": "edge_cases:0:q", "verdict": "consistent", "reason": "each row traced", "rows": rows}
    plan = _plan(edge_cases=[case])
    await pr.run_trace_critic(_FakeLLM([_report(verdict)]), _ReadGH(), plan, DEV, {})
    assert len(plan.edge_cases[0]["rejection_traces"]) == 3
    assert fr.check_pinned_messages(plan, {}, _index()) == 0


def test_the_prompt_and_schema_carry_the_first_rejecting_check():
    from src.app.llm_client import SYSTEM_PROMPT, TEST_CASE_SCHEMA

    assert "**Q, first rejecting check" in SYSTEM_PROMPT
    assert "trace EVERY ROW separately" in SYSTEM_PROMPT
    for field in ("rejection_traces", "expected_outcome"):
        assert field in TEST_CASE_SCHEMA["properties"], field


def test_log_lines_reviewer_quotes_and_telemetry_classes_are_not_pins():
    """False positives found replaying the check over 60 stored plans."""
    assert fr.pinned("The serialized log record contains `trpc request failed` and no marker.") == []
    assert fr.pinned("catches the lookup error, logs 'Active-account lookup failed' and returns unverifiable.") == []
    assert fr.pinned("The reviewer said ('the cap counts requests, not vendor cost'), so it is rejected later.") == []
    assert fr.pinned("An `$exception` event for ChunkLoadError leaves the browser.") == []
    # Backtick pairs stay aligned: prose between two code spans is not a quote.
    assert fr.pinned("After `exp` has passed, `isValidSharePreviewToken` rejects it, the draft does not resolve, "
                     "and `activate` redirects.") == []
    assert [p["kind"] for p in fr.pinned("The call throws ForbiddenError.")] == ["error_class"]


def test_observability_cases_and_already_outcome_level_text_are_skipped():
    plan = _plan(happy_path=[
        {"title": "[Log shape] failures log a redacted error", "expected": "The log line reads \"Request failed: "
         "upstream returned an error\" and the request is rejected.", "steps": ["x"], "how_to_verify": "Loki"},
        {"title": "PUT is refused", "expected": "Rejected with \"HTTP method 'PUT' is not allowed\" (exact message "
         "unverified — record what is shown).", "steps": ["x"], "how_to_verify": "body"}])
    assert fr.check_pinned_messages(plan, {}) == 0


def test_a_message_that_must_not_appear_and_test_names_are_not_pins():
    assert fr.pinned("The send fails without crashing the app into the generic \"Something went wrong\" boundary.") == []
    assert fr.pinned("The generic \"Something went wrong\" screen must not appear in either run.") == []
    assert fr.pinned("Verified against the test rows 'ML rejects bearer without anonymous retry'.") == []
    assert fr.pinned("The request fails with \"Something went wrong, try again\".") != []
