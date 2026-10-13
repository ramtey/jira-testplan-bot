"""Regeneration keeps recorded QA progress attached to the right case
(src/app/services/plan_revision.py)."""

import copy

from src.app.services.plan_revision import (
    build_progress_remap,
    map_case_ids,
    render_previous_plan,
)


def _case(title, expected="x"):
    return {"title": title, "expected": expected}


OLD = {
    "happy_path": [_case("verify script passes against Staging"),
                   _case("verify script passes against Integ")],
    "edge_cases": [_case("Invalid MODE exits 1"), _case("Stale CA bundle fails closed")],
    "integration_tests": [_case("Sibling repo still uses the insecure factory"),
                          _case("Pipeline runs from the repo root")],
    "regression_checklist": [
        "🔴 Deploy check first: confirm the environment runs merged commit abc123 or later. "
        "Staging info lists the migration history",
        "🟢 docs name the modes Integ, Staging and Prod (matching the case-sensitive filenames)",
        "🟢 A follow-up is recorded for the sibling repos",
    ],
}


def _regenerated():
    new = copy.deepcopy(OLD)
    new["edge_cases"][0]["expected"] = "Pre-existing behaviour, not changed by this ticket: ..."
    del new["integration_tests"][0]           # moved to follow-ups
    new["regression_checklist"][1] = "🟢 docs name the modes Integ, Staging and Prod"
    del new["regression_checklist"][2]        # moved to follow-ups
    return new


def test_mapping_follows_cases_across_a_removal():
    mapping = {m["from"]: m for m in map_case_ids(OLD, _regenerated())}
    assert mapping["integration_tests:1"]["to"] == "integration_tests:0"
    assert mapping["integration_tests:1"]["status"] == "moved"
    assert mapping["integration_tests:0"]["status"] == "gone"
    assert mapping["regression_checklist:2"]["status"] == "gone"
    assert mapping["regression_checklist:1"] == {**mapping["regression_checklist:1"],
                                                "to": "regression_checklist:1",
                                                "status": "reworded"}
    assert mapping["edge_cases:0"]["status"] == "kept"  # titles carry identity


def test_code_added_prefixes_do_not_break_a_match():
    new = copy.deepcopy(OLD)
    new["regression_checklist"][0] = (
        "🔴 Deploy check first: confirm the environment runs merged commit def456 or later. "
        "Unverified — assumption (no code shows this): Staging info lists the migration history"
    )
    mapping = {m["from"]: m for m in map_case_ids(OLD, new)}
    assert mapping["regression_checklist:0"]["to"] == "regression_checklist:0"


def test_remap_reports_keys_and_which_marks_need_carrying():
    remap = build_progress_remap(
        ticket_keys=["SK-1"], old_plan_id=1, old_body=OLD, new_body=_regenerated(),
        checked_ids=["happy_path:0", "integration_tests:1", "regression_checklist:1"],
    )
    assert remap["old_key"] == "SK-1:2-2-2-3"
    assert remap["new_key"] == "SK-1:2-2-1-2"
    assert remap["key_changed"] and remap["ids_changed"]
    assert {c["from"]: c["to"] for c in remap["checked"]} == {
        "happy_path:0": "happy_path:0",
        "integration_tests:1": "integration_tests:0",
        "regression_checklist:1": "regression_checklist:1",
    }
    assert remap["misattached"] == []


def test_same_shape_with_moved_cases_is_reported_as_misattached():
    # The silent case: counts unchanged, so the old row is read as-is, but the
    # passed id now names another case. mark_carryover never looks at this.
    new = copy.deepcopy(OLD)
    new["edge_cases"].reverse()
    remap = build_progress_remap(
        ticket_keys=["SK-1"], old_plan_id=1, old_body=OLD, new_body=new,
        checked_ids=["edge_cases:1"],
    )
    assert not remap["key_changed"]
    assert remap["misattached"] == [{
        "id": "edge_cases:1",
        "now_names": "Invalid MODE exits 1",
        "was": "Stale CA bundle fails closed",
        "belongs_at": "edge_cases:0",
    }]


def test_previous_plan_block_lists_ids_and_marks_passes():
    block = render_previous_plan(plan_id=7, progress_key="SK-1:2-2-2-3", body=OLD,
                                 checked_ids=["edge_cases:1"])
    assert "PREVIOUS VERSION OF THIS PLAN (plan 7, progress key SK-1:2-2-2-3)" in block
    assert "edge_cases:1 [PASSED] — Stale CA bundle fails closed" in block
    assert "edge_cases:0 — Invalid MODE exits 1" in block
    assert "regression_checklist:2 — 🟢 A follow-up is recorded" in block


def test_a_case_the_pipeline_quarantined_is_not_reported_gone():
    # 622's edge_cases:5: the model reworded it in place, labelled it an
    # assumption, and quarantine moved it to needs-spec, shifting the ids after it.
    new = copy.deepcopy(OLD)
    moved = new["edge_cases"].pop(0)
    new["needs_spec_cases"] = [moved]
    mapping = {m["from"]: m for m in map_case_ids(OLD, new)}
    assert mapping["edge_cases:0"]["status"] == "needs_spec"
    assert mapping["edge_cases:1"]["to"] == "edge_cases:0"


def test_a_line_that_changed_its_prefix_still_matches():
    # 620 -> 622 regression_checklist:0: same check, new deploy-check wording.
    # Character similarity scored 0.05; shared words carry it.
    old = {"regression_checklist": [
        "🔴 Deploy check first: confirm the environment runs merged commit abc or later. "
        "MODE=Staging ./run.sh info and MODE=Integ ./run.sh info still connect and list "
        "the full migration history with no certificate error"]}
    new = {"regression_checklist": [
        "🔴 Deploy check first: confirm the clone you run from contains merge commit abc or "
        "later (git log -1) — there is no deployed service. Then MODE=Staging ./run.sh info "
        "and MODE=Integ ./run.sh info still connect and list the full migration history "
        "with no certificate error"]}
    [m] = map_case_ids(old, new)
    assert (m["to"], m["status"], m["confident"]) == ("regression_checklist:0", "reworded", True)


def test_quarantine_spares_cases_qa_already_passed_and_note_backed_ones():
    # Plan 624: rule K made the model flag needs_manual_verification on grounded
    # cases; quarantine took happy_path from 5 to 0, three of them passed.
    from types import SimpleNamespace

    from src.app.services.plan_revision import passed_titles
    from src.app.services.test_plan_generator import quarantine_ungrounded_cases

    def ungrounded(title, **extra):
        return {"title": title, "expected": "x", "expected_verified": False,
                "needs_manual_verification": True, **extra}

    plan = SimpleNamespace(
        happy_path=[ungrounded("verify script passes against Staging"),
                    ungrounded("Note-backed case", source_comment_ids=["336591"]),
                    ungrounded("A pure guess")],
        edge_cases=[], integration_tests=[], security_negative_tests=[],
    )
    protected = passed_titles(OLD, ["happy_path:0"])
    moved = quarantine_ungrounded_cases(plan, protected_titles=protected)
    assert [c["title"] for c in moved] == ["A pure guess"]
    assert [c["title"] for c in plan.happy_path] == [
        "verify script passes against Staging", "Note-backed case"]
