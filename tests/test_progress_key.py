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

The rule has since grown a fifth number. Lifting a covered case out of the
checklist also removed its ``section:index``, so a tester who ran one anyway had
nowhere to record it — SK-2325's plan 536 rendered its whole integration section
as zero. Covered cases are addressable now, under ``covered_by_unit_test:<n>``,
and the fingerprint carries their count. It is appended only when there is at
least one, so every plan without covered cases keeps the key it already had.
"""
from __future__ import annotations

import json
import re

import pytest

from src.app.services.progress_key import (
    COVERABLE_KEYS,
    SECTION_KEYS,
    build_progress_key,
    case_index,
    fingerprint,
)


def _plan(**sections) -> str:
    return json.dumps(sections)


def test_a_case_covered_by_a_unit_test_is_not_counted_in_its_own_section():
    """It is pulled out of the manual checklist, so counting it in its section
    produces a key the UI never asks for. It is counted once, at the end, as the
    size of its own namespace."""
    body = _plan(
        happy_path=[{"title": "a"}, {"title": "b"}],
        edge_cases=[{"title": "c"}],
        integration_tests=[
            {"title": "covered", "covered_by_unit_test": True},
            {"title": "manual"},
        ],
        regression_checklist=["r1", "r2"],
    )
    assert fingerprint(body) == "2-1-1-2-1"


def test_the_sk_2642_regression():
    """The exact shape that broke: 21 stored cases, one integration case already
    covered by a unit test. The integration count must be 1, not 2 — the covered
    case is not on the manual checklist. It is named by the trailing 1."""
    body = _plan(
        happy_path=[{"title": f"h{i}"} for i in range(4)],
        edge_cases=[{"title": f"e{i}"} for i in range(8)],
        integration_tests=[
            {"title": "backend, unit-tested", "covered_by_unit_test": True},
            {"title": "backend, manual"},
        ],
        regression_checklist=[f"r{i}" for i in range(7)],
    )
    assert build_progress_key(["SK-2642"], body) == "SK-2642:4-8-1-7-1"


# ---------------------------------------------------------------------------
# The fifth number.
#
# Lifting a covered case out of the checklist also lifted it out of the *id
# space*: it had no `section:index`, so a tester who ran it anyway had nowhere
# to record that. SK-2325's plan 536 had all four integration cases flagged
# covered — the section read 0 of 0, two were verified live with real evidence,
# and mark-passed.sh answered "'integration_tests:0' is out of range —
# integration_tests has 0 case(s), valid indexes 0..-1".
#
# They are addressable now, so the key has to account for them, and these pin
# the one property that makes the change safe to ship: a plan with no covered
# cases keeps the key it already had, so nothing that is currently tracked moves.
# ---------------------------------------------------------------------------


def test_a_plan_with_no_covered_cases_keeps_its_four_number_key():
    """The migration guarantee. Writing the fifth number as `-0` would have
    re-keyed every plan in flight and orphaned every mark on every ticket."""
    body = _plan(
        happy_path=[{"title": "a"}],
        edge_cases=[{"title": "b"}, {"title": "c"}],
        integration_tests=[],
        regression_checklist=["r"],
    )
    assert fingerprint(body) == "1-2-0-1"
    assert build_progress_key(["SK-1"], body) == "SK-1:1-2-0-1"


def test_covered_cases_are_counted_across_sections_not_per_section():
    """One namespace, one number. Three counts would be more faithful and would
    also re-key any plan that merely moved a covered case between sections."""
    body = _plan(
        happy_path=[{"title": "x", "covered_by_unit_test": True}],
        edge_cases=[{"title": "y", "covered_by_unit_test": True}],
        integration_tests=[{"title": "z", "covered_by_unit_test": True}],
        regression_checklist=[],
    )
    assert fingerprint(body) == "0-0-0-0-3"


def test_the_sk_2325_plan_536_shape():
    """The plan that could not be marked: 5 happy, 8 edge, 0 integration (all
    four lifted), 9 regression. It read as `5-8-0-9` and had no id for the two
    cases a tester verified live."""
    body = _plan(
        happy_path=[{"title": f"h{i}"} for i in range(5)],
        edge_cases=[{"title": f"e{i}"} for i in range(8)],
        integration_tests=[
            {"title": f"i{i}", "covered_by_unit_test": True} for i in range(4)
        ],
        regression_checklist=[f"r{i}" for i in range(9)],
    )
    assert build_progress_key(["SK-2325"], body) == "SK-2325:5-8-0-9-4"
    assert "covered_by_unit_test:0" in case_index(body)
    assert "covered_by_unit_test:3" in case_index(body)


def test_case_index_numbers_covered_cases_in_flat_render_order():
    """`markdown.js::collectCoveredCasesWithOrigin` walks happy_path, edge_cases,
    integration_tests in that order and numbers what it finds. This is the other
    producer of that numbering, and the two must agree or a mark lands on a
    different case than the one the tester ticked."""
    body = _plan(
        happy_path=[
            {"title": "manual h"},
            {"title": "covered h", "covered_by_unit_test": True},
        ],
        edge_cases=[{"title": "covered e", "covered_by_unit_test": True}],
        integration_tests=[{"title": "covered i", "covered_by_unit_test": True}],
    )
    index = case_index(body)
    assert index["covered_by_unit_test:0"]["title"] == "covered h"
    assert index["covered_by_unit_test:1"]["title"] == "covered e"
    assert index["covered_by_unit_test:2"]["title"] == "covered i"
    assert index["covered_by_unit_test:0"]["origin_section"] == "happy_path"


def test_case_index_skips_covered_cases_when_numbering_their_own_section():
    """The ids of the manual cases must not shift when a case beside them is
    flagged covered — those ids are already recorded in `test_plan_progress`."""
    body = _plan(
        edge_cases=[
            {"title": "first manual"},
            {"title": "lifted", "covered_by_unit_test": True},
            {"title": "second manual"},
        ]
    )
    index = case_index(body)
    assert index["edge_cases:0"]["title"] == "first manual"
    assert index["edge_cases:1"]["title"] == "second manual"
    assert "edge_cases:2" not in index


def test_case_index_reads_a_regression_entry_that_is_a_bare_string():
    """Regression items are strings, not case objects — they skip every guard
    the card sections have, this one included."""
    index = case_index(_plan(regression_checklist=["Login still works"]))
    assert index["regression_checklist:0"]["title"] == "Login still works"


def test_covered_cases_are_marked_optional_in_the_index():
    """The flag the checklist denominator keys off: ticking every required case
    must still read 100% when a covered case is left unticked."""
    body = _plan(
        happy_path=[{"title": "required"}, {"title": "c", "covered_by_unit_test": True}]
    )
    index = case_index(body)
    assert index["happy_path:0"]["optional"] is False
    assert index["covered_by_unit_test:0"]["optional"] is True


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
def test_only_a_truthy_flag_moves_a_case_into_the_covered_namespace(covered):
    body = _plan(integration_tests=[{"title": "x", "covered_by_unit_test": covered}])
    assert fingerprint(body) == ("0-0-0-0-1" if covered else "0-0-1-0")


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


# ---------------------------------------------------------------------------
# The SK-2327 regression: 0% rendered over eleven real passes.
#
# The frontend derived the key from whatever plan it had on screen, so a plan
# the database had never seen — a leftover cached render, or one generated but
# not persisted — produced a key nobody writes. GET returned the 404 above, and
# the checklist rendered empty, which reads as "nothing was tested" rather than
# "your results are filed under the plan they were made against".
#
# The key now has one producer on both sides (`/plans/{id}/progress-key`), and
# these pin the lookup that lets the UI say which of the two it is.
# ---------------------------------------------------------------------------


def _client_with_rows(monkeypatch, rows):
    """A TestClient whose progress repository holds `rows` (key -> checked ids)."""
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient
    from src.app import main

    class _Row:
        def __init__(self, key, ids):
            self.progress_key = key
            self.checked_ids = json.dumps(ids)
            self.updated_at = datetime(2026, 9, 22, 21, 19, 20, tzinfo=timezone.utc)

    async def fake_find_for_same_tickets(_db, *, progress_key):
        prefix = progress_key.rsplit(":", 1)[0].upper() + ":"
        return [_Row(k, v) for k, v in rows.items() if k.startswith(prefix)]

    monkeypatch.setattr(
        main.test_plan_progress_repository,
        "find_for_same_tickets",
        fake_find_for_same_tickets,
    )
    monkeypatch.setattr(main, "get_db", lambda: object())
    return TestClient(main.app)


SK_2327 = {"SK-2327:4-5-0-6": ["happy_path:0", "happy_path:1", "edge_cases:0"]}


def test_progress_under_another_shape_is_findable(monkeypatch):
    """The exact shape that broke. The UI polled SK-2327:3-4-0-7 off a plan the
    database never had; the marks were under SK-2327:4-5-0-6. Asking about the
    empty key must surface the one that isn't."""
    c = _client_with_rows(monkeypatch, SK_2327)
    body = c.get("/test-plan-progress/SK-2327:3-4-0-7/other-shapes").json()
    assert [o["progress_key"] for o in body["others"]] == ["SK-2327:4-5-0-6"]
    assert body["others"][0]["checked_count"] == 3
    assert body["others"][0]["fingerprint"] == "4-5-0-6"


def test_the_key_asked_about_is_not_reported_as_another_shape(monkeypatch):
    """Otherwise every plan with progress would claim its own marks are orphaned."""
    c = _client_with_rows(monkeypatch, SK_2327)
    body = c.get("/test-plan-progress/SK-2327:4-5-0-6/other-shapes").json()
    assert body["others"] == []


def test_another_ticket_is_never_reported_as_another_shape(monkeypatch):
    """The prefix is the ticket(s), not a substring of them: SK-23 must not
    match SK-2327, and SK-2327 must not match the SK-2327+SK-2328 bundle."""
    c = _client_with_rows(
        monkeypatch,
        {**SK_2327, "SK-2325:4-6-2-10": ["happy_path:0"], "SK-2327+SK-2328:1-0-0-0": ["happy_path:0"]},
    )
    body = c.get("/test-plan-progress/SK-2327:3-4-0-7/other-shapes").json()
    assert [o["progress_key"] for o in body["others"]] == ["SK-2327:4-5-0-6"]


def test_a_ticket_with_no_progress_at_all_reports_nothing(monkeypatch):
    """No marks anywhere is a real state, and must stay distinguishable from
    marks filed under a previous shape — that distinction is the whole point."""
    c = _client_with_rows(monkeypatch, {})
    assert c.get("/test-plan-progress/SK-2327:3-4-0-7/other-shapes").json()["others"] == []


def test_a_corrupt_checked_ids_blob_counts_as_zero_rather_than_raising(monkeypatch):
    """A notice is an extra; it must never be able to break the plan view."""
    c = _client_with_rows(monkeypatch, {"SK-2327:4-5-0-6": ["a"]})
    from src.app import main

    async def corrupt(_db, *, progress_key):
        class _Row:
            progress_key = "SK-2327:4-5-0-6"
            checked_ids = "not json"
            updated_at = None

        return [_Row()]

    monkeypatch.setattr(
        main.test_plan_progress_repository, "find_for_same_tickets", corrupt
    )
    body = c.get("/test-plan-progress/SK-2327:3-4-0-7/other-shapes").json()
    assert body["others"][0]["checked_count"] == 0


@pytest.mark.asyncio
async def test_the_sibling_query_anchors_on_the_whole_ticket_prefix():
    """Pins the query itself, not a stand-in for it.

    The prefix is matched as ``^<TICKETS>:``. Anchored, or SK-23 would match
    SK-2327; terminated by the colon, or SK-2327 would match the SK-2327+SK-2328
    bundle — both of which would report another ticket's marks as this one's.
    ``re.escape`` matters because ``+`` is the multi-ticket separator and a live
    regex metacharacter.
    """
    from src.app.repositories import test_plan_progress_repository as repo

    captured = {}

    async def fake_find_many(_db, _model, filter_, **kwargs):
        captured["filter"] = filter_
        captured["sort"] = kwargs.get("sort")
        return []

    original = repo.crud.find_many
    repo.crud.find_many = fake_find_many
    try:
        await repo.find_for_same_tickets(object(), progress_key="SK-2327+SK-2328:4-5-0-6")
    finally:
        repo.crud.find_many = original

    pattern = captured["filter"]["progress_key"]["$regex"]
    assert re.match(pattern, "SK-2327+SK-2328:3-4-0-7")
    assert not re.match(pattern, "SK-2327:4-5-0-6")
    assert not re.match(pattern, "SK-2327+SK-2328-EXTRA:1-0-0-0")
    assert captured["sort"] == [("updated_at", -1)]
