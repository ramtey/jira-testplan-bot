"""Re-check that every expected result is something a tester can see on the deployed environment (rules AA–AG).

Plans for an HTTP service (mobile-api) predicted results the tester never saw:

* a new validation was expected to answer ``/user/%2e%2e`` with 400; on integ
  the CDN and the router resolved the dot segment first and the request got
  404 without reaching the handler;
* paths were named after the feature (``/split-and-assign/process``,
  ``/data-extract/…``) where the router registers ``/split/process`` and
  ``/extract/…``;
* a step read the stored token from ``GET /user/:id/firebase``, which never
  returns it;
* "300 requests per minute per user" was stated as exact, where integ runs 3
  pods, each with its own in-memory limiter, so about 900 get through;
* cases with an empty title or no steps, and "the repo has no test scripts"
  about a repo whose package.json has them.

The prompt has rules AA–AH for these. This module does not trust that the
generator followed them. It runs in two places, like ``plan_recheck``:

``recheck_before_routing`` (async; before ``route_out_of_uat``, so the cases it
sends to the code-review list get there)
    **AG** a case with no steps leaves the graded sections, one with no title
    is titled from its first step, and one with nothing in it is dropped.
    **AE** a case only a unit test can check leaves for the code-review list.
    **AC** a malformed-URL case that pins one status code is rewritten to the
    outcome a tester can see (rejected before downstream, any 4xx), and the
    handler's status goes to the code-review list as a unit/integration test.
    **AA/AB** every HTTP call in a step needs a ``route_sources`` entry whose
    cited file has the path's segments, and every response field a case reads
    needs a ``response_fields`` entry whose cited file has the field.

``recheck_before_save`` (sync)
    **AD** a count or limit is checked against ``environment_topology``: a
    per-pod limit on several pods becomes the range a tester sees, and an
    unknown layout makes the count "record what you see". **AE** each case says
    how it is checked. **AF** a case whose accounts or data are not confirmed is
    marked blocked by setup. **AG** claims that a repo has or lacks scripts or
    tests are checked against its package.json / build files.

Everything marks in place and is recorded in ``source_provenance["plan_recheck"]``
(the plan's "Grounding re-read" line), except the moves, which go through
``route_out_of_uat`` like rules H, L and U. Unread source never flags: a check
that could not read a cited file records it as unchecked.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from .plan_recheck import GRADED_SECTIONS, _cases, _protected, _record, _sentences

logger = logging.getLogger(__name__)

ALL_CASE_SECTIONS = GRADED_SECTIONS + ("security_negative_tests",)

ROUTE_UNCHECKED_MARKER = "Route not checked against the router (rule AA): {route} —"
ROUTE_NOT_FOUND_MARKER = "Route not found where the plan cites it (rule AA): {why} —"
FIELD_MARKER = "Response field not confirmed (rule AB): {why} —"
NOT_VISIBLE_MARKER = "Not visible from outside (rule AB): {why} —"
EDGE_OUTCOME = ("Rejected before reaching downstream services, with no downstream call. Any 4xx status passes: "
                "the CDN, load balancer, ingress or the framework's router may normalise, redirect or reject this "
                "URL before the handler runs, so record the status and which layer answered.")
EDGE_HOW = ("Record the status and the response headers (server, via, cf-ray, x-request-id) to see which layer "
            "answered, and confirm in the downstream service's logs or traces that no call was made for this "
            "request.")
EDGE_UNPINNED_MARKER = ("Unverified on a deployed environment (rule AC): the CDN or router may normalise this URL "
                        "before the handler sees it; record the status and which layer answered —")
LIMIT_UNKNOWN_MARKER = ("Count depends on the deployment layout (rule AD): {why}; record how many pass before the "
                        "first rejection —")
LIMIT_PER_POD_MARKER = "Per-pod limit (rule AD): {why} Single-pod prediction, not the pass criterion:"
SETUP_MARKER = "BLOCKED BY SETUP (rule AF): {what} — run once it exists."
UNIT_ONLY_MARKER = "Only a unit test can check this, but no test is named (rule AE) —"
REPO_CLAIM_MARKER = "{verdict} (rule AG): {why} —"

CHANNEL_LABELS = {
    "outside_http": "Checked from outside (HTTP response)",
    "outside_ui": "Checked from outside (what the app shows)",
    "outside_cli": "Checked from outside (command output)",
    "logs_or_traces": "Checked in logs or traces",
    "unit_test_only": "Checked only by a unit test",
}


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _steps(case: dict) -> list[str]:
    return [s for s in case.get("steps") or [] if isinstance(s, str) and s.strip()]


def _file_text(index, locator: str) -> str | None:
    """Whole text of the file a citation names; None when unread or unknown."""
    if index is None or isinstance(index, str) or not locator:
        return None
    try:
        got = index.resolve(locator)
    except Exception:  # noqa: BLE001 — unread is never "absent"
        return None
    text = getattr(got, "text", None)
    return text if isinstance(text, str) else None


_FILE_LINE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,6}:\d+")


# ---------------------------------------------------------------------------
# AG. Complete cases (before routing)
# ---------------------------------------------------------------------------

def _title_from(case: dict) -> str:
    src = (_steps(case) or [_text(case.get("expected"))])[0]
    src = re.sub(r"\s+", " ", src).strip().rstrip(".")
    return (src[:87] + "…") if len(src) > 88 else src


def check_complete_cases(test_plan: Any, report: dict,
                         protected_titles: frozenset[str] = frozenset()) -> int:
    """No case leaves with an empty title or no steps."""
    n = 0
    for section in ALL_CASE_SECTIONS:
        items = getattr(test_plan, section, None)
        if not isinstance(items, list):
            continue
        keep = []
        for case in items:
            if not isinstance(case, dict):
                keep.append(case)
                continue
            title = _text(case.get("title")).strip()
            steps = _steps(case)
            expected = _text(case.get("expected")).strip()
            if not title and not steps and not expected:
                n += 1
                _record(report, None, section, "AG", "moved", "removed: the case had no title, steps or expected "
                        "result", "(empty case)")
                continue
            keep.append(case)
            if _protected(case, protected_titles):
                continue
            if not title:
                case["title"] = _title_from(case)
                n += 1
                _record(report, case, section, "AG", "fixed", "the case had no title; titled from its first step")
            if not steps and not case.get("covered_by_unit_test"):
                case["not_gradeable"] = {"reason": "The plan wrote no steps for it (rule AG), so a tester cannot "
                                                   "run it."}
                n += 1
                _record(report, case, section, "AG", "moved", "no steps: a tester cannot run it")
        setattr(test_plan, section, keep)
    lines = getattr(test_plan, "regression_checklist", None)
    if isinstance(lines, list):
        test_plan.regression_checklist = [l for l in lines if not isinstance(l, str) or l.strip(" 🔴🟡🟢\t")]
    return n


# ---------------------------------------------------------------------------
# AE. Verification channel (unit-only cases move before routing; labels before save)
# ---------------------------------------------------------------------------

_SURFACE_CHANNEL = {"backend_http": "outside_http", "web_ui": "outside_ui", "mobile": "outside_ui",
                    "cli": "outside_cli"}


def _channel(case: dict) -> str | None:
    ch = case.get("verification_channel")
    if ch in CHANNEL_LABELS:
        return ch
    if case.get("covered_by_unit_test"):
        return "unit_test_only"
    return _SURFACE_CHANNEL.get(case.get("surface"))


def route_unit_only_cases(test_plan: Any, report: dict,
                          protected_titles: frozenset[str] = frozenset()) -> int:
    """A case the generator says only a unit test can check, and that no test
    proves, is not a UAT case: it goes to the code-review list."""
    n = 0
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        if case.get("verification_channel") != "unit_test_only" or case.get("covered_by_unit_test"):
            continue
        if _protected(case, protected_titles) or case.get("not_gradeable"):
            continue
        ref = _text(case.get("unit_test_ref")).strip()
        case["not_gradeable"] = {"reason": "Only a unit test can check this (rule AE); a tester cannot see it."}
        if ref and not case.get("suggested_test"):
            case["suggested_test"] = ref
        n += 1
        _record(report, case, section, "AE", "moved", "only a unit test can check it"
                + (f" ({ref})" if ref else "; no test named"))
    return n


def label_channels(test_plan: Any, report: dict) -> int:
    n = 0
    report.setdefault("channel_inferred", 0)
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        ch = _channel(case)
        how = _text(case.get("how_to_verify")).strip()
        if ch is None:
            if "(rule AE)" not in how:
                case["how_to_verify"] = (f"How this is checked is not stated (rule AE): say whether it is seen in an "
                                         f"HTTP response, the app, logs or traces, or only a unit test. {how}").strip()
                n += 1
                _record(report, case, section, "AE", "marked", "no way to check it is named")
            continue
        if case.get("verification_channel") != ch:
            case["verification_channel"] = ch
            report["channel_inferred"] += 1
        label = CHANNEL_LABELS[ch]
        if ch == "unit_test_only" and "›" not in _text(case.get("unit_test_ref")):
            exp = _text(case.get("expected"))
            if "(rule AE)" not in exp:
                case["expected"] = f"{UNIT_ONLY_MARKER} {exp}".strip()
                n += 1
                _record(report, case, section, "AE", "marked", "only a unit test can check it, and none is named")
        if not how.startswith(label):
            case["how_to_verify"] = f"{label}: {how}".strip().rstrip(":")
    return n


# ---------------------------------------------------------------------------
# AC. Malformed URLs are answered at the edge (before routing)
# ---------------------------------------------------------------------------

_URLISH_RE = re.compile(r"https?://[^\s\"'`<>()\]]+|(?<![\w.:/~-])/[^\s\"'`<>()\]]+")
_HTTP_CONTEXT_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|\bcurl\b|\brequest\b|\bnavigate\b|"
                              r"\bopen\b|\bvisit\b|\bcall\b|\bsend\b|\bhit\b|\bURL\b|\bendpoint\b|\broute\b", re.I)
_BAD_ENCODING_RE = re.compile(r"%(?:2e|2f|5c|25|00|c0|c1|e2%80|u[0-9a-f]{4})", re.I)
_LONG_PATH_TEXT_RE = re.compile(
    r"\b(?:very\s+long|over[- ]?long|oversized|extremely\s+long)\s+(?:path|url|uri)\b"
    r"|\b(?:path|url|uri)\b[^.;]{0,40}?\b\d[\d,]{3,}\s*(?:characters|chars|bytes|segments)\b"
    r"|\b\d[\d,]{3,}[- ](?:character|char|byte)\s+(?:path|url|uri)\b", re.I)
_MALFORMED_TEXT_RE = re.compile(r"\b(?:dot[- ]segments?|path\s+traversal|encoded\s+(?:slash|dot|backslash)|"
                                r"double[- ]slash(?:es)?|double[- ]encod\w*)\b", re.I)
_STATUS_PIN_RE = re.compile(
    r"\b(?:HTTP\s*|status(?:\s*code)?\s*(?:of\s*|is\s*|=\s*)?|returns?\s+(?:an?\s+)?|responds?\s+(?:with\s+)?(?:an?\s+)?|"
    r"gets?\s+(?:an?\s+)?|answers?\s+(?:with\s+)?(?:an?\s+)?)([1-5]\d\d)\b"
    r"|\b([1-5]\d\d)\s+(?:Bad Request|Unauthori[sz]ed|Forbidden|Not Found|Method Not Allowed|Conflict|Gone|"
    r"Unprocessable|Too Many|Internal Server|OK|Created|No Content|Moved|Found|Redirect)", re.I)


def malformed_url_reason(path: str) -> str | None:
    """Why a URL path would be normalised or rejected before the handler, or None."""
    if not path:
        return None
    if _BAD_ENCODING_RE.search(path):
        return f"encoded character {_BAD_ENCODING_RE.search(path).group(0)!r} in the path"
    segs = path.split("/")
    if any(s in (".", "..") for s in segs[1:]):
        return "a '.' or '..' path segment"
    if "//" in path:
        return "a double slash in the path"
    if "\\" in path:
        return "a backslash in the path"
    if any(ord(c) > 127 for c in path):
        return "a non-ASCII character in the path"
    if len(path) > 1024:
        return f"a {len(path)}-character path"
    return None


def _malformed_inputs(case: dict) -> list[tuple[str, str]]:
    """``[(url or phrase, why)]`` for malformed URLs a case sends."""
    out: list[tuple[str, str]] = []
    texts = _steps(case) + [_text(case.get("test_data"))]
    req = case.get("request")
    if isinstance(req, dict):
        texts.append(" ".join(str(v) for v in req.values() if isinstance(v, (str, int))))
    elif isinstance(req, str):
        texts.append(req)
    for t in texts:
        if not t:
            continue
        http = bool(_HTTP_CONTEXT_RE.search(t))
        for m in _URLISH_RE.finditer(t):
            tok = m.group(0).rstrip(".,;:\\")  # a JSON-escaped quote leaves a trailing backslash
            is_url = tok.lower().startswith(("http://", "https://"))
            if not (is_url or http):
                continue
            # Keep the raw path (urlsplit would not decode, but strip scheme + host).
            path = tok
            if is_url:
                try:
                    parts = urlsplit(tok)
                except ValueError:
                    continue
                path = tok[len(f"{parts.scheme}://{parts.netloc}"):] or "/"
            path = re.split(r"[?#]", path, 1)[0]
            why = malformed_url_reason(path)
            if why:
                out.append((tok if len(tok) <= 80 else tok[:77] + "…", why))
        if http:
            for rx in (_LONG_PATH_TEXT_RE, _MALFORMED_TEXT_RE):
                m = rx.search(t)
                if m:
                    out.append((m.group(0), m.group(0).lower()))
    return list(dict.fromkeys(out))


def _pinned_statuses(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) or m.group(2) for m in _STATUS_PIN_RE.finditer(text or "")))


def check_malformed_urls(test_plan: Any, report: dict,
                         protected_titles: frozenset[str] = frozenset()) -> int:
    n = 0
    review = getattr(test_plan, "code_review_checks", None)
    review = review if isinstance(review, list) else []
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        if case.get("surface") == "cli" or case.get("covered_by_unit_test") or _protected(case, protected_titles):
            continue
        expected = _text(case.get("expected"))
        if "(rule AC)" in expected or expected.startswith(EDGE_OUTCOME[:40]):
            continue
        inputs = _malformed_inputs(case)
        if not inputs:
            continue
        if _FILE_LINE_RE.search(_text(case.get("reaches_handler_source"))):
            continue  # the plan read config proving the request reaches the handler unchanged
        pinned = _pinned_statuses(expected)
        if not pinned:
            continue
        what = inputs[0][0]
        errors = [s for s in pinned if s[0] in "45"]
        if errors:
            original = expected
            case["expected"] = (f"{EDGE_OUTCOME} (rule AC: the plan predicted {', '.join(pinned)} from the handler; "
                                f"the handler's own status is checked by a unit or integration test, not here.)")
            case["expected_outcome"] = EDGE_OUTCOME
            how = _text(case.get("how_to_verify")).strip()
            if EDGE_HOW not in how:
                case["how_to_verify"] = f"{how} {EDGE_HOW}".strip()
            source = _text(case.pop("expected_source", "")).strip()
            case["expected_verified"] = False
            review.append({
                "title": f"The handler answers {what} with {', '.join(errors)}",
                "why_not_uat": ("On a deployed environment the CDN, load balancer, ingress or router can normalise "
                                f"or reject this URL ({inputs[0][1]}) before the handler runs, so a tester cannot see "
                                "the handler's own status (rule AC)."),
                "what_to_verify": original,
                "suggested_test": _text(case.get("unit_test_ref")).strip() or (
                    f"A unit or integration test that calls the handler directly (not through the router) with "
                    f"{what} and asserts {', '.join(errors)}."),
                "code_refs": [source] if source else [],
            })
            n += 1
            _record(report, case, section, "AC", "downgraded",
                    f"pinned {', '.join(pinned)} for a malformed URL ({inputs[0][1]}); the edge can answer first, so "
                    f"any 4xx passes and the handler status moved to a unit/integration test", what)
        else:
            case["expected"] = f"{EDGE_UNPINNED_MARKER} {expected}"
            case["expected_verified"] = False
            n += 1
            _record(report, case, section, "AC", "marked",
                    f"expects {', '.join(pinned)} for a malformed URL ({inputs[0][1]}) that the edge may rewrite", what)
    if review:
        test_plan.code_review_checks = review
    return n


# ---------------------------------------------------------------------------
# AA. Routes come from the router; AB. response fields from the response code
# ---------------------------------------------------------------------------

_CALL_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+[`\"']?(?:https?://[^\s/`\"']+)?(/[^\s`\"'<>,;)]*)")
_CURL_RE = re.compile(r"\bcurl\b(?P<args>[^\n]*?)(?P<url>https?://[^\s`\"']+)")
_PARAM_SEG_RE = re.compile(r"^(?::\w+\??|\{[^}]*\}|<[^>]*>|\[[^\]]*\]|\*\w*|\$\w+|_+)$")
_HTTP_SECTIONS_SURFACES = ("backend_http",)


def _calls(step: str) -> list[tuple[str, str]]:
    out = [(m.group(1).upper(), m.group(2)) for m in _CALL_RE.finditer(step)]
    for m in _CURL_RE.finditer(step):
        meth = re.search(r"-X\s*([A-Z]+)|--request\s+([A-Z]+)", m.group("args") or "")
        method = (meth.group(1) or meth.group(2)) if meth else ("POST" if re.search(r"\s-d\b|--data", step) else "GET")
        path = urlsplit(m.group("url")).path or "/"
        out.append((method.upper(), path))
    return list(dict.fromkeys((meth, re.split(r"[?#]", p, 1)[0].rstrip(".,:")) for meth, p in out))


def _segments(path: str) -> list[str]:
    return [s for s in path.strip().split("/") if s]


def route_matches(pattern: str, path: str) -> bool:
    """Does a called path match a registered pattern? Params match any segment."""
    a, b = _segments(pattern.lower()), _segments(path.lower())
    if a and a[-1] in ("*", "**", "(.*)"):
        return len(b) >= len(a) - 1 and all(_seg_eq(x, y) for x, y in zip(a[:-1], b))
    return len(a) == len(b) and all(_seg_eq(x, y) for x, y in zip(a, b))


def _seg_eq(pattern_seg: str, seg: str) -> bool:
    return bool(_PARAM_SEG_RE.match(pattern_seg) or _PARAM_SEG_RE.match(seg) or pattern_seg == seg)


def _static_segments(pattern: str) -> list[str]:
    return [s for s in _segments(pattern) if not _PARAM_SEG_RE.match(s) and not s.startswith("*")]


def _needs_http_checks(section: str, case: dict) -> bool:
    return section == "security_negative_tests" or case.get("surface") in _HTTP_SECTIONS_SURFACES


def route_locators(test_plan: Any) -> list[str]:
    """Files the route and response citations name, to read before checking."""
    locs = []
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        for r in case.get("route_sources") or []:
            if isinstance(r, dict):
                locs += [_text(r.get("source")), _text(r.get("mount_source"))]
        for f in case.get("response_fields") or []:
            if isinstance(f, dict):
                locs.append(_text(f.get("source")))
    return [l for l in dict.fromkeys(locs) if l and _FILE_LINE_RE.search(l)]


def gateway_prefixes(test_plan: Any) -> set[str]:
    """Path prefixes something in front of the service strips or rewrites
    ("Envoy gateway, /api/ → /"), read from the plan's environment_topology."""
    out: set[str] = set()
    for t in getattr(test_plan, "environment_topology", None) or []:
        if isinstance(t, dict):
            for m in re.finditer(r"(?<![\w:/])/([\w.-]+)/?\s*(?:→|->|=>|\bto\b|\brewritten\b|\bstripped\b)",
                                 _text(t.get("in_front"))):
                out.add(m.group(1).lower())
    return out


def _route_problem(method: str, path: str, routes: list[dict], index,
                   prefixes: set[str] = frozenset()) -> tuple[str, str, str] | None:
    """``(marker, reason, action)`` for a call the route citations do not back;
    ``("", "", "unchecked")`` when the cited file could not be read; None when fine."""
    segs = _segments(path)
    paths = [path] + (["/" + "/".join(segs[1:])] if segs and segs[0].lower() in prefixes else [])
    match = next((r for r in routes for p in paths if route_matches(_text(r.get("path")), p)
                  and _text(r.get("method")).upper() in (method, "ALL", "ANY", "")), None)
    if match is None:
        return (ROUTE_UNCHECKED_MARKER.format(route=f"{method} {path}"),
                f"{method} {path}: no route_sources entry backs it", "marked")
    src, mount = _text(match.get("source")), _text(match.get("mount_source"))
    if not _FILE_LINE_RE.search(src):
        return ROUTE_NOT_FOUND_MARKER.format(why="no <file>:<line> cited"), f"{method} {path}: no file:line cited", "marked"
    src_text = _file_text(index, src)
    if src_text is None:
        return "", "", "unchecked"
    # File-based routers (Next.js, SvelteKit, Nuxt) keep the path in the file's own path.
    blob = "\n".join([src_text, _file_text(index, mount) or "", src.split(":")[0], mount.split(":")[0]]).lower()
    missing = [s for s in _static_segments(_text(match.get("path"))) if s.lower() not in blob]
    if missing:
        why = f"'{missing[0]}' of {_text(match.get('path'))} is not in {src}" + (f" or {mount}" if mount else "")
        return ROUTE_NOT_FOUND_MARKER.format(why=why), f"{method} {path}: {why}", "downgraded"
    return None


def check_routes(test_plan: Any, report: dict, index=None) -> int:
    """Marks each step whose call is not backed, and records one finding per case."""
    n = 0
    unchecked = 0
    prefixes = gateway_prefixes(test_plan)
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        if not _needs_http_checks(section, case):
            continue
        routes = [r for r in case.get("route_sources") or [] if isinstance(r, dict) and _text(r.get("path"))]
        steps = case.get("steps") or []
        reasons, action = [], "marked"
        for i, step in enumerate(steps):
            if not isinstance(step, str) or "(rule AA)" in step:
                continue
            for method, path in _calls(step):
                problem = _route_problem(method, path, routes, index, prefixes)
                if problem is None:
                    continue
                marker, why, act = problem
                if act == "unchecked":
                    unchecked += 1
                    continue
                steps[i] = f"{marker} {step}"
                reasons.append(f"step {i + 1}: {why}")
                action = "downgraded" if act == "downgraded" else action
                break
        if reasons:
            n += 1
            _record(report, case, section, "AA", action, "; ".join(reasons)[:300], reasons[0])
    if unchecked:
        report.setdefault("not_run", []).append(f"{unchecked} cited route file(s) could not be read, so those routes "
                                                f"were not re-checked")
    return n

_BODY_FIELD_RE = re.compile(
    r"\b(?:response|body|payload|json)\b[^.;]{0,60}?\b(?:contains?|includes?|has|have|returns?|with|field|property|key)\b"
    r"[^.;]{0,30}?[`\"']([A-Za-z_][\w.\[\]]*)[`\"']"
    r"|\b(?:response|res|body|data)\.([A-Za-z_]\w*(?:\.\w+)*)", re.I)


def check_response_fields(test_plan: Any, report: dict, index=None) -> int:
    n = 0
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        fields = [f for f in case.get("response_fields") or [] if isinstance(f, dict) and _text(f.get("field"))]
        expected = _text(case.get("expected"))
        if "(rule AB)" in expected:
            continue
        problems, notes = [], []
        for f in fields:
            name, src = _text(f.get("field")), _text(f.get("source"))
            if f.get("returned") is False:
                alt = _text(f.get("alternate_check")).strip()
                if alt:
                    notes.append(f"'{name}' is not in the response ({src or 'source not cited'}); check it via: {alt}")
                else:
                    problems.append((NOT_VISIBLE_MARKER, f"the API does not return '{name}' ({src or 'no source'}) "
                                                         f"and no other way to check it is given"))
                continue
            text = _file_text(index, src)
            leaf = re.split(r"[.\[\]]", name.strip("[]."))
            leaf = [s for s in leaf if s][-1:] or [name]
            if not _FILE_LINE_RE.search(src):
                problems.append((FIELD_MARKER, f"'{name}' cites no <file>:<line> for the code that builds the response"))
            elif text is not None and leaf[0].lower() not in text.lower():
                problems.append((FIELD_MARKER, f"'{name}' is not in {src}"))
        if not fields and _needs_http_checks(section, case):
            m = _BODY_FIELD_RE.search(expected)
            if m:
                problems.append((FIELD_MARKER, f"reads '{m.group(1) or m.group(2)}' from the response without "
                                               f"checking the code that builds it"))
        if notes:
            how = _text(case.get("how_to_verify")).strip()
            add = " ".join(x for x in notes if x not in how)
            if add:
                case["how_to_verify"] = f"{how} {add}".strip()
        if problems:
            marker, why = problems[0]
            case["expected"] = f"{marker.format(why=why)} {expected}".strip()
            case["expected_verified"] = False
            n += 1
            for _, w in problems:
                _record(report, case, section, "AB", "downgraded", w)
    return n


async def check_routes_and_shapes(test_plan: Any, report: dict, *, index=None, github=None) -> int:
    """AA and AB, reading the cited router and response files first."""
    if index is not None and not isinstance(index, str) and github is not None:
        from .source_facts import read_cited

        try:
            await read_cited(index, github, route_locators(test_plan))
        except Exception as e:  # noqa: BLE001 — checks below treat unread as unchecked
            logger.warning("could not read cited route files: %s", e)
    return check_routes(test_plan, report, index) + check_response_fields(test_plan, report, index)


# ---------------------------------------------------------------------------
# AD. Counts and limits follow the environment layout (before save)
# ---------------------------------------------------------------------------

_LIMIT_RE = re.compile(
    r"\b(\d[\d,]*)\s*(?:requests?|calls?|attempts?|reqs?|hits?|messages?|logins?|tries|uploads?|sends?)\s*"
    r"(?:per|/|an?|every|each|in\s+(?:a|one|1))\s*(?:user\s+)?(?:per\s+)?"
    r"(second|sec|s|minute|min|m|hour|hr|h|day|d)\b"
    r"|\brate[- ]limit(?:ed)?\s+(?:of|at|to)\s+(\d[\d,]*)\b"
    r"|\b(\d[\d,]*)(?:st|nd|rd|th)\s+(?:request|call|attempt)\b", re.I)
_LIMITER_WORDS = re.compile(r"rate|limit|throttl|quota|bucket|counter", re.I)
# A count only matters here when the case expects the service to start
# rejecting; a vendor's limit or an alert threshold is not this service's.
_REJECTION_WORDS = re.compile(r"\b429\b|too many requests|rate[- ]limit|throttl|rejected|blocked|quota", re.I)
_NOT_OURS = re.compile(r"\b(?:upstream|vendor|third[- ]party|provider's|external\s+(?:api|service)|alert(?:ing)?\s+rule|"
                       r"threshold|retryable|retri(?:ed|es)|fall(?:s|en)?\s+back|transient)\b", re.I)
_PER_POD_TEXT = re.compile(r"\bper[- ]pod\b|\beach pod\b|\bper instance\b|\bper replica\b", re.I)


def _int(s: str) -> int | None:
    try:
        return int(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _replica_bounds(v: Any) -> tuple[int, int] | None:
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))]
    if not nums:
        return None
    return min(nums), max(nums)


def _topology_for(test_plan: Any, case: dict) -> dict | None:
    topo = [t for t in getattr(test_plan, "environment_topology", None) or [] if isinstance(t, dict)]
    if not topo:
        return None
    env = _text(case.get("environment")).lower()
    named = [t for t in topo if _text(t.get("environment")).strip()
             and _text(t.get("environment")).strip().lower() in env]
    if named:
        return named[0]
    return topo[0] if len(topo) == 1 else None


def check_limits(test_plan: Any, report: dict) -> int:
    n = 0
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        expected = _text(case.get("expected"))
        if "(rule AD)" in expected or case.get("covered_by_unit_test"):
            continue
        m = _LIMIT_RE.search(expected)
        if not m or not _REJECTION_WORDS.search(expected) or _NOT_OURS.search(expected):
            continue
        limit = _int(m.group(1) or m.group(3) or m.group(4) or "")
        if m.group(4) and limit:
            limit -= 1  # "the 301st request" means a limit of 300
        topo = _topology_for(test_plan, case)
        bounds = _replica_bounds(topo.get("replicas")) if topo else None
        states = [s for s in (topo or {}).get("state") or [] if isinstance(s, dict)]
        limiter = next((s for s in states if _LIMITER_WORDS.search(_text(s.get("what")))), None)
        if limiter is None and len(states) == 1:
            limiter = states[0]
        if topo is None or bounds is None or limiter is None or limiter.get("scope") not in ("per_pod", "shared"):
            if _PER_POD_TEXT.search(expected):
                continue
            why = ("the plan did not read how many pods serve this environment" if topo is None or bounds is None
                   else "the plan did not read whether the limit's state is in memory per pod or shared")
            case["expected"] = f"{LIMIT_UNKNOWN_MARKER.format(why=why)} {expected}"
            case["expected_verified"] = False
            n += 1
            _record(report, case, section, "AD", "marked", why, m.group(0))
            continue
        lo_pods, hi_pods = bounds
        if limiter.get("scope") == "shared" or hi_pods <= 1 or _PER_POD_TEXT.search(expected):
            continue
        env = _text(topo.get("environment")) or "this environment"
        rsrc = _text(topo.get("replicas_source")) or "source not cited"
        ssrc = _text(limiter.get("source")) or "source not cited"
        pods = str(hi_pods) if lo_pods == hi_pods else f"{lo_pods}–{hi_pods}"
        rng = (f"between {limit} and {limit * hi_pods}" if limit else f"up to {hi_pods} times the single-pod limit")
        why = (f"{env} runs {pods} pods ({rsrc}) and the {_text(limiter.get('what')) or 'limiter'} is in memory per "
               f"pod ({ssrc}), so {rng} pass before the first rejection, depending on how the load balancer spreads "
               f"them; record the count.")
        case["expected"] = f"{LIMIT_PER_POD_MARKER.format(why=why)} {expected}"
        n += 1
        _record(report, case, section, "AD", "downgraded", why, m.group(0))
    return n


# ---------------------------------------------------------------------------
# AF. Accounts and data each case needs (before save)
# ---------------------------------------------------------------------------

_SPECIAL_DATA_RE = re.compile(
    r"\b(?:second|another|other|different|foreign)\s+(?:tenant|organi[sz]ation|org|account|brokerage|office|team|"
    r"company|customer)\b"
    r"|\b(?:deactivated|disabled|suspended|deleted|soft[- ]deleted|locked|inactive|expired)\s+(?:user|account|"
    r"subscription|member)\b"
    r"|\b(?:no|without\s+(?:a|an)?)\s*(?:subscriber|subscription|payment method|license|seat)\b", re.I)


def check_setup(test_plan: Any, report: dict) -> int:
    n = 0
    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        pre = _text(case.get("preconditions"))
        if "(rule AF)" in pre:
            continue
        setup = [s for s in case.get("required_setup") or [] if isinstance(s, dict) and _text(s.get("need"))]
        blocked = _text(case.get("blocked_by_setup")).strip()
        if not blocked:
            open_items = [s for s in setup if s.get("status") != "exists"]
            if open_items:
                blocked = "; ".join(f"{_text(s.get('need'))} ({s.get('status') or 'unconfirmed'}"
                                    + (f": {_text(s.get('how_to_get'))}" if _text(s.get("how_to_get")) else "") + ")"
                                    for s in open_items)
            elif not setup:
                hay = " ".join(_steps(case) + [pre, _text(case.get("test_data")), _text(case.get("credentials"))])
                hit = _SPECIAL_DATA_RE.search(hay)
                if hit:
                    blocked = (f"needs {hit.group(0).strip()}, which the plan did not confirm exists — confirm it, "
                               f"or ask who owns the test data")
        if not blocked:
            continue
        case["blocked_by_setup"] = blocked
        case["preconditions"] = f"{SETUP_MARKER.format(what=blocked)} {pre}".strip()
        n += 1
        _record(report, case, section, "AF", "marked", f"blocked by setup: {blocked}"[:200])
    return n


# ---------------------------------------------------------------------------
# AG. Claims about a repo's scripts and tests come from its files (before save)
# ---------------------------------------------------------------------------

_TEST_NOUN = (r"(?:(?:test|unit|e2e|npm|yarn|pnpm|build|lint)\s+)?(?:scripts?|tests?|test\s+(?:suites?|runners?|"
              r"targets?|projects?|scripts?)|runnable\s+test\s+projects?|specs?)")
# What may follow the noun: the end of the clause, or a word that keeps the
# claim about the repo ("no test runner at merge commit …"), never a noun that
# makes it something else ("no CAR TEST schema").
_NOUN_END = r"(?=\s*(?:$|[.,;:)—–-])|\s+(?:in|at|and|or|so|because|exist|are|is|defined|configured)\b)"
_REPO_CLAIM_RE = re.compile(
    r"\b(?:has|have|contains?|defines?|ships?)\s+no\s+(?:\w+\s+){0,1}?" + _TEST_NOUN + _NOUN_END
    + r"|\bno\s+(?:test|npm|yarn|pnpm|build|lint|e2e|unit)\s+(?:scripts?|suites?|targets?|runners?)" + _NOUN_END
    + r"|\b(?:does\s+not|doesn't|do\s+not|don't)\s+(?:have|define|contain|include)\s+(?:a\s+|any\s+)?(?:\w+\s+){0,1}?"
    + _TEST_NOUN + _NOUN_END
    + r"|\bthere\s+(?:are|is)\s+no\s+(?:\w+\s+){0,1}?" + _TEST_NOUN + _NOUN_END, re.I)
# Coverage claims ("no test file for the changed code", "no test covering X")
# are about which code a test exercises — rules C, D and S — not about the
# repo's scripts; and a sentence a code check already marked is code's own.
_COVERAGE_CLAIM = re.compile(r"\bchanged code\b|\bcover(?:s|ing|age)?\b|\bexercis\w*|\bassert\w*|\bshown\b|"
                             r"\bfor\s+(?:the\s+)?(?:changed|new|this|that|these|either)\b|\(rule [A-Z]{1,2}\)", re.I)


def _repo_facts(repo_evidence: list[dict] | None) -> list[dict]:
    return [e for e in repo_evidence or [] if isinstance(e, dict)]


def _contradiction(sentence: str, evidence: list[dict]) -> tuple[str, str] | None:
    """``(verdict, why)`` for a "no scripts / no tests" sentence, or None when it holds."""
    named = [e for e in evidence if e.get("repo") and e["repo"].split("/")[-1].lower() in sentence.lower()]
    scope = named or (evidence if len(evidence) == 1 else [])
    about_tests = bool(re.search(r"\btests?\b|\bspecs?\b|\bsuites?\b", sentence, re.I))
    if not scope:
        if not evidence:
            return ("Unverified", "the repo's package.json and build files were not read")
        scope = evidence  # several repos, none named: contradicted only if every repo contradicts it
        all_repos = True
    else:
        all_repos = False
    reasons = []
    for e in scope:
        facts = e.get("commands") or {}
        scripts = facts.get("package_scripts")
        tests = list((e.get("test_sources") or {}).keys())
        android = facts.get("android_apps") or []
        why = None
        if scripts and (not about_tests or any("test" in s or "spec" in s for s in scripts)):
            shown = [s for s in scripts if "test" in s or "spec" in s] if about_tests else scripts
            why = f"{e.get('repo')}'s package.json defines {', '.join(shown[:8])}"
        elif about_tests and tests:
            why = f"{e.get('repo')} has test files, e.g. {', '.join(tests[:3])}"
        elif about_tests and android:
            why = f"{e.get('repo')}'s Gradle build has per-variant unit test tasks ({android[0].get('file')})"
        if why:
            reasons.append(why)
        elif all_repos:
            return None
        elif not facts and not tests:
            return ("Unverified", f"{e.get('repo')}'s package.json and build files were not read")
    if reasons:
        return ("Contradicted by the repo's files", "; ".join(reasons))
    return None


def check_repo_claims(test_plan: Any, report: dict, repo_evidence: list[dict] | None) -> int:
    evidence = _repo_facts(repo_evidence)
    n = 0

    def fix(text: str) -> tuple[str, str | None]:
        if not text or "(rule AG)" in text:
            return text, None
        out, hit = [], None
        for s in _sentences(text):
            if hit is None and _REPO_CLAIM_RE.search(s) and not _COVERAGE_CLAIM.search(s):
                verdict = _contradiction(s, evidence)
                if verdict:
                    hit = verdict[1]
                    out.append(f"{REPO_CLAIM_MARKER.format(verdict=verdict[0], why=verdict[1])} {s}")
                    continue
            out.append(s)
        return (" ".join(out), hit) if hit else (text, None)

    for section, _, case in _cases(test_plan, ALL_CASE_SECTIONS):
        for key in ("expected", "preconditions", "how_to_verify", "unit_test_gap"):
            new, why = fix(_text(case.get(key)))
            if why:
                case[key] = new
                n += 1
                _record(report, case, section, "AG", "marked", why)
        steps = case.get("steps") or []
        for i, st in enumerate(steps):
            new, why = fix(_text(st))
            if why:
                steps[i] = new
                n += 1
                _record(report, case, section, "AG", "marked", why)
    for gap in getattr(test_plan, "risks_and_gaps", None) or []:
        if not isinstance(gap, dict):
            continue
        for key in ("gap", "impact"):
            new, why = fix(_text(gap.get(key)))
            if why:
                gap[key] = new
                n += 1
                _record(report, None, "risks_and_gaps", "AG", "marked", why, _text(gap.get("gap")))
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        new, why = fix(_text(line))
        if why:
            lines[i] = new
            n += 1
            _record(report, None, "regression_checklist", "AG", "marked", why, _text(line))
    return n


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def recheck_before_routing(test_plan: Any, report: dict, *, index=None, github=None,
                                 protected_titles: frozenset[str] = frozenset()) -> None:
    """AG (complete cases), AE (unit-only cases), AC, AA, AB."""
    check_complete_cases(test_plan, report, protected_titles)
    route_unit_only_cases(test_plan, report, protected_titles)
    check_malformed_urls(test_plan, report, protected_titles)
    await check_routes_and_shapes(test_plan, report, index=index, github=github)


def recheck_before_save(test_plan: Any, report: dict, *, repo_evidence: list[dict] | None = None) -> None:
    """AD, AE (labels), AF, AG (repo claims)."""
    check_limits(test_plan, report)
    label_channels(test_plan, report)
    check_setup(test_plan, report)
    check_repo_claims(test_plan, report, repo_evidence)

