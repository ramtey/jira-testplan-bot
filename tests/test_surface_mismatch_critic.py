"""Tests for the post-generation surface-mismatch critic — SK-2546-style drift.

The critic reads the classifier's output (deliverable + verification
surface + off-target signals) and each test case (title, steps,
expected), and returns an on_surface/off_surface verdict per case.
Off-surface cases get badged with ``needs_manual_verification=True``
and gain a matching ``grounding_warnings`` entry so the frontend
renders them under the existing "Unverified UI" badge.

Regression coverage is anchored on the real SK-2546 example: a
"Pilot App: Update App Store Images" ticket whose default plan told
QA to compare a Drive PNG against the running app (a designer-fidelity
check) instead of verifying the swap in App Store Connect.
"""

from src.app.deliverable_classifier import Deliverable
from src.app.models import TestPlan
from src.app.surface_mismatch_critic import (
    apply_surface_verdicts,
    build_case_surface_inputs,
    build_surface_critic_user_message,
    parse_surface_verdicts,
    should_run,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _sk2546_deliverable() -> Deliverable:
    """Deliverable the classifier returned for SK-2546 in the dry-run.

    The specific wording matters less than the shape: an
    ``app_store_asset`` whose surface is App Store Connect (NOT the
    running app), with explicit off-target signals against launching
    the app and against comparing Drive vs live app UI.
    """
    return Deliverable(
        artifact_type="app_store_asset",
        deliverable=(
            "Updated first-slide screenshot for the Pilot app's App Store listing, "
            "reflecting current Popular Calculators UI."
        ),
        verification_surface=(
            "App Store Connect Media Manager for the Pilot app, and the live App "
            "Store product page after metadata approval."
        ),
        anchor_steps=[
            "Open App Store Connect → Pilot app → Screenshots and confirm the first slide "
            "has been replaced with the updated image from the Drive folder.",
            "After metadata approval, view the Pilot listing on the App Store and verify "
            "the first screenshot displays the updated design.",
        ],
        off_target_signals=[
            "Do not launch the Pilot app to check the Popular Calculators section — "
            "no runtime code changed.",
            "Do not compare the Drive PNG against the live app UI — that's a "
            "designer-fidelity check, not this ticket's scope.",
        ],
    )


def _sk2546_plan() -> TestPlan:
    """The plan the tool actually posted to SK-2546 (paraphrased into the
    dict shape the critic ingests).

    - happy_path[0]: compare Drive image to running app → OFF-surface
      (designer-fidelity, explicit off-target signal).
    - edge_cases[0]: check image dimensions in Drive → OFF-surface
      (verifies source artifact, not the surface).
    - edge_cases[1]: launch the app and check Popular Calculators →
      OFF-surface (launches the app on a no-code ticket).
    """
    return TestPlan(
        happy_path=[
            {
                "title": "Verify first-slide App Store image matches live Pilot app",
                "steps": [
                    "Access the Google Drive folder containing the updated images.",
                    "Open the first slide image.",
                    "Compare its Popular Calculators section against the live Pilot app.",
                ],
                "expected": "The Drive image matches the current live app.",
                "covers_acs": [],
            },
        ],
        edge_cases=[
            {
                "title": "Image dimensions meet Apple App Store screenshot requirements",
                "steps": [
                    "Download the updated first slide image from the Drive folder.",
                    "Check the image dimensions.",
                ],
                "expected": "Image is 1290 x 2796 (iPhone 15 Pro Max) or an accepted size.",
                "covers_acs": [],
            },
            {
                "title": "Pilot app launches and displays Popular Calculators correctly",
                "steps": [
                    "Launch the Pilot app.",
                    "Verify the Popular Calculators section renders correctly.",
                ],
                "expected": "The Popular Calculators section matches what the screenshot depicts.",
                "covers_acs": [],
            },
        ],
        regression_checklist=[],
        integration_tests=[],
    )


# ─── should_run ───────────────────────────────────────────────────────────────


def test_should_run_true_for_valid_asset_deliverable():
    assert should_run(_sk2546_deliverable()) is True


def test_should_run_false_when_deliverable_missing():
    assert should_run(None) is False


def test_should_run_false_for_code_behavior():
    """For runtime code changes the existing three critics already anchor
    cases; the surface critic adds noise more than signal."""
    d = _sk2546_deliverable()
    d.artifact_type = "code_behavior"
    assert should_run(d) is False


def test_should_run_false_for_unknown():
    """When the classifier hedged with ``unknown``, we don't trust the
    surface enough to badge cases against it."""
    d = _sk2546_deliverable()
    d.artifact_type = "unknown"
    assert should_run(d) is False


def test_should_run_false_when_surface_blank():
    d = _sk2546_deliverable()
    d.verification_surface = "   "
    assert should_run(d) is False


# ─── build_case_surface_inputs ────────────────────────────────────────────────


def test_build_inputs_collects_all_sections():
    plan = _sk2546_plan()
    inputs = build_case_surface_inputs(plan)
    ids = [c["case_id"] for c in inputs]
    assert ids == ["happy_path:0", "edge_cases:0", "edge_cases:1"]


def test_build_inputs_skips_already_badged_cases():
    """Cases the earlier critics already flagged shouldn't be re-checked —
    the badge is already there and re-checking wastes budget."""
    plan = _sk2546_plan()
    plan.edge_cases[0]["needs_manual_verification"] = True
    inputs = build_case_surface_inputs(plan)
    ids = [c["case_id"] for c in inputs]
    assert "edge_cases:0" not in ids
    assert "edge_cases:1" in ids


def test_build_inputs_skips_empty_cases():
    """A case with neither a title nor any usable steps carries no signal."""
    plan = TestPlan(
        happy_path=[{"title": "", "steps": [], "expected": ""}],
        edge_cases=[],
        regression_checklist=[],
    )
    assert build_case_surface_inputs(plan) == []


def test_build_inputs_truncates_long_steps():
    """A 1000-char step would balloon the critic payload; the builder must
    cap each step so the payload stays predictable."""
    long_step = "x" * 1000
    plan = TestPlan(
        happy_path=[{"title": "case", "steps": [long_step], "expected": ""}],
        edge_cases=[],
        regression_checklist=[],
    )
    inputs = build_case_surface_inputs(plan)
    assert len(inputs) == 1
    assert len(inputs[0]["steps"][0]) < len(long_step)
    assert inputs[0]["steps"][0].endswith("…")


# ─── build_surface_critic_user_message ────────────────────────────────────────


def test_user_message_includes_deliverable_context():
    plan = _sk2546_plan()
    cases = build_case_surface_inputs(plan)
    msg = build_surface_critic_user_message(cases, _sk2546_deliverable())
    assert "Artifact type: app_store_asset" in msg
    assert "Updated first-slide screenshot" in msg
    assert "App Store Connect Media Manager" in msg


def test_user_message_lists_anchor_steps_and_off_targets():
    plan = _sk2546_plan()
    cases = build_case_surface_inputs(plan)
    msg = build_surface_critic_user_message(cases, _sk2546_deliverable())
    assert "Open App Store Connect" in msg
    assert "Do not launch the Pilot app" in msg
    assert "designer-fidelity check" in msg


def test_user_message_renders_every_case_id():
    plan = _sk2546_plan()
    cases = build_case_surface_inputs(plan)
    msg = build_surface_critic_user_message(cases, _sk2546_deliverable())
    assert "── CASE happy_path:0 ──" in msg
    assert "── CASE edge_cases:0 ──" in msg
    assert "── CASE edge_cases:1 ──" in msg


# ─── parse_surface_verdicts ───────────────────────────────────────────────────


def test_parse_verdicts_from_tool_input_dict():
    raw = {
        "verdicts": [
            {"case_id": "happy_path:0", "verdict": "off_surface", "reason": "…"},
            {"case_id": "edge_cases:0", "verdict": "on_surface", "reason": "…"},
        ]
    }
    out = parse_surface_verdicts(raw)
    assert out["happy_path:0"]["verdict"] == "off_surface"
    assert out["edge_cases:0"]["verdict"] == "on_surface"


def test_parse_verdicts_from_list():
    raw = [
        {"case_id": "happy_path:0", "verdict": "off_surface", "reason": "…"},
    ]
    out = parse_surface_verdicts(raw)
    assert out["happy_path:0"]["verdict"] == "off_surface"


def test_parse_verdicts_drops_malformed_entries():
    raw = {
        "verdicts": [
            {"case_id": "happy_path:0", "verdict": "off_surface", "reason": "ok"},
            {"case_id": "", "verdict": "off_surface", "reason": "bad — empty id"},
            {"case_id": "edge_cases:0", "verdict": "invalid", "reason": "bad enum"},
            "not a dict",
            {"case_id": "edge_cases:1"},
        ]
    }
    out = parse_surface_verdicts(raw)
    assert set(out.keys()) == {"happy_path:0"}


def test_parse_verdicts_returns_empty_on_junk():
    """Whatever the LLM throws at us, we return {} rather than raising."""
    assert parse_surface_verdicts(None) == {}
    assert parse_surface_verdicts("garbage") == {}
    assert parse_surface_verdicts({"verdicts": "not a list"}) == {}


# ─── apply_surface_verdicts ───────────────────────────────────────────────────


def test_apply_verdicts_badges_off_surface_cases():
    plan = _sk2546_plan()
    d = _sk2546_deliverable()
    verdicts = {
        "happy_path:0": {"verdict": "off_surface", "reason": "designer-fidelity check"},
        "edge_cases:1": {"verdict": "off_surface", "reason": "launches the app"},
    }
    added = apply_surface_verdicts(plan, verdicts, d)

    assert plan.happy_path[0]["needs_manual_verification"] is True
    assert plan.edge_cases[1]["needs_manual_verification"] is True
    # edge_cases[0] wasn't in the verdict dict — should stay untouched.
    assert plan.edge_cases[0].get("needs_manual_verification") is not True

    assert len(added) == 2
    # ac_id is a synthetic "surface:<artifact_type>" tag since surface
    # warnings don't map to a specific numbered AC.
    assert all(w["ac_id"] == "surface:app_store_asset" for w in added)
    assert all(w["source"] == "critic_surface" for w in added)
    assert all(w["severity"] == "warn" for w in added)
    # The explanation must carry the "Surface critic:" prefix so operators
    # can tell this warning came from the surface pass, not fix-scope.
    assert all(w["explanation"].startswith("Surface critic:") for w in added)


def test_apply_verdicts_leaves_on_surface_cases_alone():
    plan = _sk2546_plan()
    d = _sk2546_deliverable()
    verdicts = {
        "happy_path:0": {"verdict": "on_surface", "reason": "opens App Store Connect"},
    }
    added = apply_surface_verdicts(plan, verdicts, d)
    assert added == []
    assert plan.happy_path[0].get("needs_manual_verification") is not True


def test_apply_verdicts_empty_dict_short_circuits():
    plan = _sk2546_plan()
    d = _sk2546_deliverable()
    added = apply_surface_verdicts(plan, {}, d)
    assert added == []


def test_apply_verdicts_falls_back_to_generic_reason_when_llm_omits_it():
    """If the LLM returns off_surface with no reason, the warning still has
    to carry SOMETHING actionable — otherwise a QA reader sees a blank
    badge and can't act on it."""
    plan = _sk2546_plan()
    d = _sk2546_deliverable()
    verdicts = {"happy_path:0": {"verdict": "off_surface", "reason": ""}}
    added = apply_surface_verdicts(plan, verdicts, d)
    assert len(added) == 1
    assert d.verification_surface in added[0]["explanation"]


def test_apply_verdicts_appends_to_existing_warnings():
    """Earlier critics (grounding, fix-scope) may have already added
    warnings. Surface warnings must be appended, not replace them."""
    plan = _sk2546_plan()
    plan.grounding_warnings = [
        {
            "ac_id": "SK-2546-AC1",
            "missing_element": "unrelated",
            "explanation": "AC critic: ...",
            "source": "critic_ac",
            "severity": "warn",
        }
    ]
    d = _sk2546_deliverable()
    verdicts = {"happy_path:0": {"verdict": "off_surface", "reason": "…"}}
    apply_surface_verdicts(plan, verdicts, d)
    assert len(plan.grounding_warnings) == 2
    assert plan.grounding_warnings[0]["source"] == "critic_ac"
    assert plan.grounding_warnings[1]["source"] == "critic_surface"


# ─── determinism ──────────────────────────────────────────────────────────────


def test_user_message_is_deterministic():
    """No timestamps, no hash-order iteration — same inputs → same output.
    Matters for cache hits and for snapshot-style test-plan diffing."""
    plan = _sk2546_plan()
    cases = build_case_surface_inputs(plan)
    a = build_surface_critic_user_message(cases, _sk2546_deliverable())
    b = build_surface_critic_user_message(cases, _sk2546_deliverable())
    assert a == b
