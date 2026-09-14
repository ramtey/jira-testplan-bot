"""The progress key must have exactly one definition.

`test_plan_progress` is keyed by ticket key(s) plus a fingerprint of the plan's
section sizes. That key had two producers that silently drifted apart:

* the frontend counted sections *after* removing cases flagged
  ``covered_by_unit_test`` (they are lifted out of the manual checklist), and
* ``uat-runner/scripts/mark-passed.sh`` built the key from four numbers typed by
  whoever ran it.

Commit 5332f54 introduced the filtering on the frontend side and did not touch
the script, so for any plan containing a covered case the two disagreed. SK-2642
was marked under ``SK-2642:4-8-2-7`` while the UI polled ``SK-2642:4-8-1-7`` and
showed 0/20 — ten passed cases invisible to the reviewing QA, with no error
anywhere, because the GET returned 200 and an empty set for every key.

These tests pin the counting rule and the 404, which together make a wrong key
impossible to write silently.
"""
from __future__ import annotations

import json

import pytest

from src.app.services.progress_key import (
    COVERABLE_KEYS,
    SECTION_KEYS,
    build_progress_key,
    fingerprint,
)


def _plan(**sections) -> str:
    return json.dumps(sections)


def test_a_case_covered_by_a_unit_test_is_not_counted():
    """It is pulled out of the manual checklist, so counting it produces a key
    the UI never asks for."""
    body = _plan(
        happy_path=[{"title": "a"}, {"title": "b"}],
        edge_cases=[{"title": "c"}],
        integration_tests=[
            {"title": "covered", "covered_by_unit_test": True},
            {"title": "manual"},
        ],
        regression_checklist=["r1", "r2"],
    )
    assert fingerprint(body) == "2-1-1-2"


def test_the_sk_2642_regression():
    """The exact shape that broke: 21 stored cases, one integration case already
    covered by a unit test. The key must be 4-8-1-7, not 4-8-2-7."""
    body = _plan(
        happy_path=[{"title": f"h{i}"} for i in range(4)],
        edge_cases=[{"title": f"e{i}"} for i in range(8)],
        integration_tests=[
            {"title": "backend, unit-tested", "covered_by_unit_test": True},
            {"title": "backend, manual"},
        ],
        regression_checklist=[f"r{i}" for i in range(7)],
    )
    assert build_progress_key(["SK-2642"], body) == "SK-2642:4-8-1-7"


def test_the_regression_checklist_is_never_filtered():
    """Only card sections carry the covered flag. A stray flag on a regression
    entry must not silently shrink the count."""
    assert "regression_checklist" not in COVERABLE_KEYS
    body = _plan(
        happy_path=[],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[{"title": "r", "covered_by_unit_test": True}],
    )
    assert fingerprint(body) == "0-0-0-1"


def test_section_order_is_fixed():
    """The fingerprint is positional — reordering silently remaps every key."""
    assert SECTION_KEYS == (
        "happy_path",
        "edge_cases",
        "integration_tests",
        "regression_checklist",
    )


def test_missing_sections_count_as_zero():
    assert fingerprint(_plan(happy_path=[{"t": 1}])) == "1-0-0-0"


def test_a_non_json_plan_body_degrades_to_zeros_rather_than_raising():
    """Markdown- and jira-format plans have no sections. They should not be able
    to crash a progress lookup."""
    assert fingerprint("# Just a markdown plan") == "0-0-0-0"
    assert fingerprint(None) == "0-0-0-0"


def test_multi_ticket_keys_join_in_run_order():
    body = _plan(happy_path=[{"t": 1}])
    assert build_progress_key(["SK-1", "SK-2"], body) == "SK-1+SK-2:1-0-0-0"


def test_ticket_keys_are_upper_cased():
    """The repository upper-cases on read and write; the key must match or the
    lookup misses."""
    body = _plan(happy_path=[{"t": 1}])
    assert build_progress_key(["sk-2642"], body) == "SK-2642:1-0-0-0"


@pytest.mark.parametrize("covered", [True, False])
def test_only_a_truthy_flag_removes_a_case(covered):
    body = _plan(integration_tests=[{"title": "x", "covered_by_unit_test": covered}])
    assert fingerprint(body) == ("0-0-0-0" if covered else "0-0-1-0")


# ---------------------------------------------------------------------------
# The 404. Returning 200 + an empty set for every key is what let a wrong key
# go unnoticed: mark-passed.sh documents "a non-200 GET means the key is wrong"
# as its only guard, and that guard could never fire.
# ---------------------------------------------------------------------------


def _client(monkeypatch, stored):
    """A TestClient whose progress repository knows about `stored` keys only."""
    from fastapi.testclient import TestClient
    from src.app import main

    class _Row:
        def __init__(self, ids):
            self.checked_ids = json.dumps(ids)
            self.updated_at = None

    async def fake_get_progress(_db, *, progress_key):
        return _Row(stored[progress_key]) if progress_key in stored else None

    monkeypatch.setattr(
        main.test_plan_progress_repository, "get_progress", fake_get_progress
    )
    monkeypatch.setattr(main, "get_db", lambda: object())
    return TestClient(main.app)


def test_an_unknown_progress_key_is_404_not_an_empty_set(monkeypatch):
    c = _client(monkeypatch, {})
    r = c.get("/test-plan-progress/SK-2642:9-9-9-9")
    assert r.status_code == 404, (
        "an unknown key must be distinguishable from a real plan with no checks — "
        "otherwise a mistyped key writes QA results somewhere nothing reads"
    )


def test_a_known_progress_key_still_returns_its_checks(monkeypatch):
    c = _client(monkeypatch, {"SK-2642:4-8-1-7": ["tc-happy_path-0"]})
    r = c.get("/test-plan-progress/SK-2642:4-8-1-7")
    assert r.status_code == 200
    assert r.json()["checked_ids"] == ["tc-happy_path-0"]
