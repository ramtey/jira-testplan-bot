"""Tests for the pre-plan deliverable classifier.

The classifier's job is to name the ticket's deliverable (what changes)
and the verification surface (where a tester observes the change), so
the generator can anchor cases correctly and the surface critic has
something to check against.

Coverage anchors on the SK-2546 regression: a "Pilot App: Update App
Store Images" ticket whose deliverable is an App Store screenshot
upload, but whose default plan told QA to compare a Drive PNG against
the running app — a designer-fidelity check, not a verification of the
actual work item.
"""

from src.app.deliverable_classifier import (
    ARTIFACT_TYPES,
    Deliverable,
    aggregate_deliverables_for_critique,
    build_classifier_user_message,
    format_deliverable_hint,
    parse_deliverable,
)


# ─── parse_deliverable ────────────────────────────────────────────────────────


def test_parse_deliverable_happy_path():
    """SK-2546-shape classification round-trips into a Deliverable."""
    raw = {
        "artifact_type": "app_store_asset",
        "deliverable": "Updated first-slide screenshot for the Pilot App Store listing.",
        "verification_surface": "App Store Connect + the live App Store product page.",
        "anchor_steps": [
            "Open App Store Connect → Pilot → Screenshots and confirm the swap.",
            "After metadata approval, open the App Store on device and verify.",
        ],
        "off_target_signals": [
            "Do not launch the app to check Popular Calculators.",
        ],
    }

    d = parse_deliverable(raw)

    assert d is not None
    assert d.artifact_type == "app_store_asset"
    assert d.deliverable.startswith("Updated first-slide")
    assert "App Store Connect" in d.verification_surface
    assert len(d.anchor_steps) == 2
    assert d.off_target_signals == ["Do not launch the app to check Popular Calculators."]


def test_parse_deliverable_rejects_unknown_artifact_type():
    """An artifact_type not in the enum → None, so the pipeline degrades cleanly."""
    raw = {
        "artifact_type": "made_up_type",
        "deliverable": "…",
        "verification_surface": "…",
    }
    assert parse_deliverable(raw) is None


def test_parse_deliverable_rejects_missing_deliverable():
    """A blank deliverable string is unusable — the hint would be empty."""
    raw = {
        "artifact_type": "code_behavior",
        "deliverable": "   ",
        "verification_surface": "The account screen in iOS Simulator.",
    }
    assert parse_deliverable(raw) is None


def test_parse_deliverable_rejects_missing_surface():
    """No verification_surface → nothing for the critic to check against."""
    raw = {
        "artifact_type": "code_behavior",
        "deliverable": "Fix flex-1 on the account-screen name wrapper.",
    }
    assert parse_deliverable(raw) is None


def test_parse_deliverable_rejects_non_dict():
    assert parse_deliverable(None) is None
    assert parse_deliverable("string") is None
    assert parse_deliverable([1, 2, 3]) is None


def test_parse_deliverable_drops_bad_list_items():
    """Non-string entries in anchor_steps / off_target_signals are dropped
    silently — the LLM occasionally emits odd shapes and we'd rather ship
    with a partial hint than fail the request."""
    raw = {
        "artifact_type": "documentation",
        "deliverable": "Rewrite the README setup section.",
        "verification_surface": "The rendered README on GitHub.",
        "anchor_steps": ["Open the README on GitHub.", 42, "", None],
        "off_target_signals": [123, "Do not run make install."],
    }
    d = parse_deliverable(raw)
    assert d is not None
    assert d.anchor_steps == ["Open the README on GitHub."]
    assert d.off_target_signals == ["Do not run make install."]


def test_parse_deliverable_handles_missing_lists():
    """The lists are optional; the parser must tolerate their absence."""
    raw = {
        "artifact_type": "config_flag",
        "deliverable": "LaunchDarkly flag `pilot-uat-2026` set to 100% for prod.",
        "verification_surface": "The LaunchDarkly dashboard.",
    }
    d = parse_deliverable(raw)
    assert d is not None
    assert d.anchor_steps == []
    assert d.off_target_signals == []


# ─── format_deliverable_hint ──────────────────────────────────────────────────


def _hint_for(artifact_type: str, off_targets=None, anchors=None) -> str:
    return format_deliverable_hint(
        Deliverable(
            artifact_type=artifact_type,
            deliverable="A short one-liner.",
            verification_surface="A short surface description.",
            anchor_steps=list(anchors or []),
            off_target_signals=list(off_targets or []),
        )
    )


def test_hint_includes_core_fields():
    hint = _hint_for("app_store_asset")
    assert "Artifact type: `app_store_asset`" in hint
    assert "A short one-liner." in hint
    assert "A short surface description." in hint


def test_hint_renders_anchor_and_off_target_bullets():
    hint = _hint_for(
        "app_store_asset",
        anchors=["Open App Store Connect.", "Verify on the live App Store."],
        off_targets=["Do not launch the app.", "Do not check other slides."],
    )
    assert "Open App Store Connect." in hint
    assert "Verify on the live App Store." in hint
    assert "Do not launch the app." in hint
    assert "Do not check other slides." in hint


def test_hint_warns_no_runtime_code_for_non_code_artifacts():
    """The "no runtime code" line is critical: it's what steers the generator
    away from writing "launch the app" cases on asset/config/doc tickets."""
    for t in ARTIFACT_TYPES:
        if t == "code_behavior":
            continue
        hint = _hint_for(t)
        assert "does not change runtime code" in hint, f"missing no-code line for {t}"


def test_hint_omits_no_runtime_code_line_for_code_behavior():
    """For real code changes, the "no runtime code" line would be wrong —
    that's exactly the case where the generator SHOULD launch the app."""
    hint = _hint_for("code_behavior")
    assert "does not change runtime code" not in hint


# ─── build_classifier_user_message ────────────────────────────────────────────


def test_classifier_user_message_names_ticket_and_summary():
    msg = build_classifier_user_message(
        ticket_key="SK-2546",
        summary="Pilot App: Update App Store Images",
        description="Outdated UI on the first slide of the App Store images…",
        issue_type="Story",
    )
    assert "TICKET: SK-2546" in msg
    assert "TYPE: Story" in msg
    assert "Pilot App: Update App Store Images" in msg
    assert "Outdated UI on the first slide" in msg


def test_classifier_user_message_declares_no_files_when_no_prs():
    """SK-2546 has no linked PR — the classifier must be told explicitly,
    since "no code changed" is the strongest signal for a non-code
    artifact type."""
    msg = build_classifier_user_message(
        ticket_key="SK-2546",
        summary="Pilot App: Update App Store Images",
        description="…",
        development_info=None,
    )
    assert "FILES CHANGED (from linked PRs): none" in msg


def test_classifier_user_message_renders_pr_titles_and_files():
    dev = {
        "pull_requests": [
            {
                "title": "Fix account-screen name overflow",
                "files_changed": [
                    {"filename": "apps/expo/src/app/account.tsx"},
                    {"filename": "apps/expo/src/app/other.tsx"},
                ],
            },
        ],
    }
    msg = build_classifier_user_message(
        ticket_key="SK-2545",
        summary="Long user names get cut off",
        description="…",
        development_info=dev,
    )
    assert "Fix account-screen name overflow" in msg
    assert "apps/expo/src/app/account.tsx" in msg
    assert "apps/expo/src/app/other.tsx" in msg
    # And the "no files" line must NOT appear when files are present.
    assert "FILES CHANGED (from linked PRs): none" not in msg


def test_classifier_user_message_truncates_long_description():
    """A 20k-char description would blow the classifier's context; the
    builder must cap it. The exact cap is an implementation detail, so
    we just assert the payload is bounded."""
    huge = "x" * 20_000
    msg = build_classifier_user_message(
        ticket_key="SK-9999",
        summary="ignored",
        description=huge,
    )
    # The message is well under the raw description length once truncated.
    assert len(msg) < 10_000
    # And truncation leaves an ellipsis marker so it's obvious to a reader.
    assert "…" in msg


# ─── aggregate_deliverables_for_critique ──────────────────────────────────────


def _asset(key: str = "SK-2546") -> Deliverable:
    return Deliverable(
        artifact_type="app_store_asset",
        deliverable=f"Updated screenshot for {key}.",
        verification_surface="App Store Connect.",
        anchor_steps=["Open App Store Connect."],
        off_target_signals=["Do not launch the app."],
    )


def _code(key: str = "SK-2545") -> Deliverable:
    return Deliverable(
        artifact_type="code_behavior",
        deliverable=f"flex-1 fix in {key}.",
        verification_surface="Account screen in iOS Simulator.",
        anchor_steps=["Open the app and log in as a long-name user."],
        off_target_signals=[],
    )


def _doc(key: str = "SK-9000") -> Deliverable:
    return Deliverable(
        artifact_type="documentation",
        deliverable=f"README rewrite for {key}.",
        verification_surface="The rendered README on GitHub.",
        anchor_steps=["Open the README on GitHub."],
        off_target_signals=["Do not run make install."],
    )


def test_aggregate_returns_none_for_empty_input():
    assert aggregate_deliverables_for_critique([]) is None


def test_aggregate_returns_none_when_all_are_unknown():
    d = Deliverable(
        artifact_type="unknown",
        deliverable="…",
        verification_surface="…",
    )
    assert aggregate_deliverables_for_critique([("SK-1", d)]) is None


def test_aggregate_returns_none_when_all_are_none():
    assert aggregate_deliverables_for_critique([("SK-1", None), ("SK-2", None)]) is None


def test_aggregate_skips_when_batch_mixes_code_and_asset():
    """The mixed case is exactly where the surface critic would false-positive
    on the code ticket's legitimate "launch the app" cases — the guard is
    load-bearing."""
    per_ticket = [("SK-2545", _code()), ("SK-2546", _asset())]
    assert aggregate_deliverables_for_critique(per_ticket) is None


def test_aggregate_single_deliverable_prefixes_ticket_key():
    """Even with one deliverable, the ticket key gets threaded into the
    surface line so critic verdicts can name which ticket a case drifted
    from."""
    result = aggregate_deliverables_for_critique([("SK-2546", _asset("SK-2546"))])
    assert result is not None
    assert result.artifact_type == "app_store_asset"
    assert "(SK-2546)" in result.verification_surface
    assert result.anchor_steps == ["(SK-2546) Open App Store Connect."]
    assert result.off_target_signals == ["(SK-2546) Do not launch the app."]


def test_aggregate_multiple_non_code_deliverables_synthesizes():
    """A doc + asset batch — both non-code — folds into one deliverable
    the critic can check against, with both surfaces joined and both
    off-target sets unioned."""
    result = aggregate_deliverables_for_critique([
        ("SK-2546", _asset("SK-2546")),
        ("SK-9000", _doc("SK-9000")),
    ])
    assert result is not None
    # Two asset-type = 1, doc-type = 1 → tie; first-seen (asset) wins.
    assert result.artifact_type == "app_store_asset"
    assert "(SK-2546)" in result.verification_surface
    assert "(SK-9000)" in result.verification_surface
    # Anchor steps and off-targets from both tickets are present, prefixed.
    assert "(SK-2546) Open App Store Connect." in result.anchor_steps
    assert "(SK-9000) Open the README on GitHub." in result.anchor_steps
    assert "(SK-2546) Do not launch the app." in result.off_target_signals
    assert "(SK-9000) Do not run make install." in result.off_target_signals


def test_aggregate_dominant_type_wins_when_not_tied():
    """When one artifact_type is strictly more common, it wins the
    ``surface:<type>`` tag so the badge is informative."""
    result = aggregate_deliverables_for_critique([
        ("SK-A", _doc("SK-A")),
        ("SK-B", _doc("SK-B")),
        ("SK-C", _asset("SK-C")),
    ])
    assert result is not None
    assert result.artifact_type == "documentation"


def test_aggregate_dedupes_repeated_signals_across_tickets():
    """Two tickets with byte-identical off-target signals (after key prefix)
    shouldn't produce a doubled list. The prefix means they SHOULD stay
    distinct when the ticket keys differ — dedup only when the tagged form
    would collide."""
    d1 = _asset("SK-1")
    d2 = _asset("SK-2")
    # Same raw off-target text but different ticket key → both survive.
    result = aggregate_deliverables_for_critique([("SK-1", d1), ("SK-2", d2)])
    assert result is not None
    assert "(SK-1) Do not launch the app." in result.off_target_signals
    assert "(SK-2) Do not launch the app." in result.off_target_signals


def test_classifier_message_reminds_no_code_means_no_app():
    """The trailing reminder is what pushes the classifier off its "the
    surface is the running app" prior — without it, SK-2546-shape tickets
    still get misclassified."""
    msg = build_classifier_user_message(
        ticket_key="SK-2546",
        summary="Pilot App: Update App Store Images",
        description="…",
    )
    assert "if no code changed, the verification surface is NOT the running app" in msg
