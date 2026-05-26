"""Tests for cross-project seam extraction."""

from src.app.seam_extractor import (
    build_seam_catalog,
    classify_multi_ticket_mode,
    extract_calls,
    extract_exports,
    normalize_route,
)


def _patch(*added_lines: str) -> str:
    """Build a minimal unified-diff patch with the given added lines."""
    body = "\n".join(f"+{ln}" for ln in added_lines)
    return f"@@ -0,0 +1,{len(added_lines)} @@\n{body}"


def _ticket(repo: str, filename: str, patch: str) -> dict:
    return {
        "development_info": {
            "pull_requests": [
                {
                    "repository": repo,
                    "files_changed": [
                        {"filename": filename, "status": "added", "patch": patch}
                    ],
                }
            ]
        }
    }


# ── normalize_route ─────────────────────────────────────────────────────────


def test_normalize_route_collapses_param_styles():
    assert normalize_route("/orders/:id") == "/orders/{}"
    assert normalize_route("/orders/{id}") == "/orders/{}"
    assert normalize_route("/orders/42") == "/orders/{}"
    assert normalize_route("/users/{userId}/orders/{orderId}") == "/users/{}/orders/{}"


def test_normalize_route_strips_query_and_trailing_slash():
    assert normalize_route("/foo/?q=1") == "/foo"
    assert normalize_route("/") == "/"


# ── extract_exports ─────────────────────────────────────────────────────────


def test_extract_fastapi_route():
    patch = _patch('@app.get("/api/orders/{order_id}")', "async def get_order(order_id):")
    exports = extract_exports("agent-calculator", "src/main.py", patch)
    assert len(exports) == 1
    assert exports[0].kind == "http_route"
    assert exports[0].identifier == "GET /api/orders/{}"
    assert exports[0].file == "src/main.py"
    assert exports[0].repo == "agent-calculator"


def test_extract_express_route():
    patch = _patch('router.post("/quote", handler)')
    exports = extract_exports("agent-calculator", "server/routes.js", patch)
    assert len(exports) == 1
    assert exports[0].kind == "http_route"
    assert exports[0].identifier == "POST /quote"


def test_extract_event_publish():
    patch = _patch('await publish("order.created", payload)')
    exports = extract_exports("checkout", "src/events.ts", patch)
    assert any(e.kind == "event" and e.identifier == "order.created" for e in exports)


def test_extract_skips_removed_files_via_catalog():
    # build_seam_catalog should skip status=removed; verify via the catalog API
    tickets = [
        {
            "development_info": {
                "pull_requests": [
                    {
                        "repository": "files-ui",
                        "files_changed": [
                            {
                                "filename": "old.py",
                                "status": "removed",
                                "patch": _patch('@app.get("/old")'),
                            }
                        ],
                    }
                ]
            }
        }
    ]
    cat = build_seam_catalog(tickets)
    # Removed file's export should not appear
    assert all(
        s.identifier != "GET /old" for s in cat.verified + cat.suspected
    )


def test_extract_handles_empty_patch():
    assert extract_exports("any", "any.py", "") == []
    assert extract_calls("any", "any.py", "") == []


# ── extract_calls ───────────────────────────────────────────────────────────


def test_extract_fetch_call():
    patch = _patch('await fetch("/api/orders/42")')
    calls = extract_calls("compliance", "src/api.ts", patch)
    assert any(
        c.kind == "http_route" and c.identifier == "GET /api/orders/{}" for c in calls
    )


def test_extract_axios_call_with_method():
    patch = _patch('axios.post("/quote", body)')
    calls = extract_calls("compliance", "src/api.ts", patch)
    assert any(
        c.kind == "http_route" and c.identifier == "POST /quote" for c in calls
    )


def test_extract_skips_non_route_urls():
    patch = _patch(
        'fetch("data:image/png;base64,abc")',
        'fetch("mailto:foo@bar.com")',
        'fetch("//cdn.example.com/asset.js")',
    )
    calls = extract_calls("any", "any.ts", patch)
    assert calls == []


def test_extract_package_import_requires_scope_allowlist():
    patch = _patch('import { computeTax } from "@acme/billing"')
    # No scopes configured → not picked up
    assert extract_calls("compliance", "src/tax.ts", patch) == []
    # Scope configured → picked up
    calls = extract_calls(
        "compliance", "src/tax.ts", patch, package_scopes=["@acme"]
    )
    assert any(c.identifier == "@acme/billing" for c in calls)


def test_extract_event_subscribe():
    patch = _patch('queue.subscribe("order.created", handle_order)')
    calls = extract_calls("notifications", "src/worker.py", patch)
    assert any(c.kind == "event" and c.identifier == "order.created" for c in calls)


# ── build_seam_catalog ──────────────────────────────────────────────────────


def test_verified_seam_when_producer_and_consumer_match():
    tickets = [
        _ticket("agent-calculator", "src/main.py", _patch('@app.get("/quote")')),
        _ticket("compliance", "src/client.ts", _patch('await fetch("/quote")')),
    ]
    cat = build_seam_catalog(tickets)
    assert len(cat.verified) == 1
    seam = cat.verified[0]
    assert seam.identifier == "GET /quote"
    assert seam.producer["repo"] == "agent-calculator"
    assert seam.consumer["repo"] == "compliance"
    assert seam.verified is True


def test_path_normalization_matches_across_styles():
    tickets = [
        _ticket(
            "agent-calculator",
            "src/main.py",
            _patch('@app.get("/orders/{order_id}")'),
        ),
        _ticket(
            "compliance",
            "src/client.ts",
            _patch('await fetch("/orders/42")'),
        ),
    ]
    cat = build_seam_catalog(tickets)
    assert len(cat.verified) == 1
    assert cat.verified[0].identifier == "GET /orders/{}"


def test_call_without_matching_export_is_suspected():
    tickets = [
        _ticket("compliance", "src/client.ts", _patch('await fetch("/orphan")')),
    ]
    cat = build_seam_catalog(tickets)
    assert cat.verified == []
    assert len(cat.suspected) == 1
    s = cat.suspected[0]
    assert s.verified is False
    assert s.consumer["repo"] == "compliance"
    assert s.producer is None


def test_export_without_matching_call_is_suspected():
    tickets = [
        _ticket("agent-calculator", "src/main.py", _patch('@app.get("/lonely")')),
    ]
    cat = build_seam_catalog(tickets)
    assert cat.verified == []
    assert len(cat.suspected) == 1
    s = cat.suspected[0]
    assert s.producer["repo"] == "agent-calculator"
    assert s.consumer is None


def test_same_repo_export_and_call_does_not_create_seam():
    # An export and a call inside the SAME repo aren't a cross-project seam.
    tickets = [
        _ticket(
            "files-ui",
            "server/app.py",
            _patch('@app.get("/internal")', 'fetch("/internal")'),
        ),
    ]
    cat = build_seam_catalog(tickets)
    assert cat.verified == []


def test_catalog_lists_repos():
    tickets = [
        _ticket("agent-calculator", "src/main.py", _patch('@app.get("/quote")')),
        _ticket("compliance", "src/client.ts", _patch('await fetch("/quote")')),
    ]
    cat = build_seam_catalog(tickets)
    assert cat.repos == ["agent-calculator", "compliance"]


def test_empty_input_yields_empty_catalog():
    assert build_seam_catalog([]).is_empty
    assert build_seam_catalog([{}]).is_empty


# ── classify_multi_ticket_mode ──────────────────────────────────────────────


def test_classify_single_repo():
    tickets = [
        _ticket("files-ui", "a.py", _patch("x = 1")),
        _ticket("files-ui", "b.py", _patch("y = 2")),
    ]
    assert classify_multi_ticket_mode(tickets) == "single_repo"


def test_classify_cross_project():
    tickets = [
        _ticket("files-ui", "a.py", _patch("x = 1")),
        _ticket("compliance", "b.py", _patch("y = 2")),
    ]
    assert classify_multi_ticket_mode(tickets) == "cross_project"


def test_classify_ignores_tickets_without_dev_info():
    tickets = [
        {"development_info": None},
        _ticket("only-one", "a.py", _patch("x = 1")),
    ]
    assert classify_multi_ticket_mode(tickets) == "single_repo"
