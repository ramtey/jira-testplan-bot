"""An error response a client must handle has more than one body shape.

On SK-2719 a downstream 401 for an invalid token came back with an EMPTY body,
and the service's error middleware passed status and body straight through
to the iOS app. The plan's case about how the app handles those errors only
asked for the documented JSON error shape, so the response that actually
reaches users in that path was never tested. The same holds for any client:
gateways and proxies answer with HTML or plain text, upstreams answer 401/502
with nothing at all, and a client that only decodes the happy error shape
crashes, hangs or shows a raw decode error on the others.

The generator prompt asks for the variants (grounding rule 1). This check is
the part that does not depend on the model: every case that checks how a
client handles error responses, and does not already cover both variants, gets
a step asking for the ones it is missing. It adds a step, never a case, so
section sizes — the progress key — are unchanged.
"""

from __future__ import annotations

import re
from typing import Any

# Security negative cases test the server's deny path, not a client's handling of it.
CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests")

_CLIENT = re.compile(r"\b(?:app|client|iOS|Android|UI|front-?end|browser|SDK|caller|consumer|screen|web)\b", re.I)
_ERROR_RESPONSE = re.compile(
    r"\b(?:[45]\d\d|non-2xx|error (?:response|body|status|payload|shape)s?|failure status(?:es)?|"
    r"error middleware|errorMiddleware|downstream (?:error|failure)s?|failed request)\b", re.I)
# How the client copes, not merely that it "shows" something.
_HANDLING = re.compile(
    r"\b(?:handled (?:error|failure)|handles? (?:the |an |each )?(?:error|failure)s?|error handling|"
    r"crash\w*|decod\w*|readable error|error (?:state|message|screen|toast|banner|alert)|"
    r"(?:render|show|display|surface)s? (?:a |an |the )?(?:readable |handled |generic )?error)\b", re.I)
_EMPTY = re.compile(r"\b(?:empty(?:[- ]body| response| payload)|no (?:response )?body|"
                    r"content-length:?\s*0|zero-length)\b", re.I)
_NON_JSON = re.compile(r"\b(?:non-?JSON|text/html|text/plain|HTML (?:body|page|error)|plain[- ]text|"
                       r"not JSON|invalid JSON|malformed JSON)\b", re.I)

ADDED_MARKER = "Error-body variants"


def _text(case: dict) -> str:
    parts = [case.get("title"), case.get("expected"), case.get("preconditions"), case.get("test_data"),
             *(case.get("steps") or [])]
    return " ".join(p for p in parts if isinstance(p, str))


def checks_client_error_handling(case: dict) -> bool:
    """A case about how a client copes with an error response."""
    t = _text(case)
    return bool(_ERROR_RESPONSE.search(t) and _CLIENT.search(t) and _HANDLING.search(t))


def missing_variants(case: dict) -> list[str]:
    t = _text(case)
    out = []
    if not _EMPTY.search(t):
        out.append("empty")
    if not _NON_JSON.search(t):
        out.append("non_json")
    return out


def variant_step(missing: list[str]) -> str:
    shapes = []
    if "empty" in missing:
        shapes.append("an EMPTY body (Content-Length: 0 — e.g. a downstream 401 for an invalid or "
                      "expired token, which an error middleware that forwards the downstream response "
                      "passes through as is)")
    if "non_json" in missing:
        shapes.append("a NON-JSON body (an HTML or plain-text error page from a gateway or proxy, "
                      "e.g. a 502 while the upstream is stopped)")
    return (f"{ADDED_MARKER}: repeat the failure so the client receives "
            + " and ".join(f"({chr(97 + i)}) {s}" for i, s in enumerate(shapes))
            + ". For each, record the status and Content-Type the client got and confirm it shows a "
              "handled error — not a crash, a raw decode error, a blank screen or a hang. If a variant "
              "cannot be produced in this environment, record that instead of passing it.")


def require_error_body_variants(test_plan: Any) -> list[str]:
    """Add the missing empty-body / non-JSON step to client error-handling cases.

    Returns the ids changed. A case that already names both variants is left
    alone, and so is one this check already extended.
    """
    changed = []
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if not isinstance(case, dict) or case.get("covered_by_unit_test"):
                continue
            if any(isinstance(s, str) and s.startswith(ADDED_MARKER) for s in case.get("steps") or []):
                continue
            if not checks_client_error_handling(case):
                continue
            missing = missing_variants(case)
            if not missing:
                continue
            case["steps"] = list(case.get("steps") or []) + [variant_step(missing)]
            case["error_body_variants_added"] = missing
            changed.append(f"{section}:{i}")
    return changed
