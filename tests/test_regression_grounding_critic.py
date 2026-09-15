"""Tests for the regression-grounding critic — SK-2342's phantom controls.

Plan 522 (SK-2342, run 923) shipped two regression-checklist lines that
asserted against UI affordances the app does not have:

    regression_checklist:4
      "🟡 Text, Print and Download share options from the share menu still
       work and are unaffected by the audio-language change."
    regression_checklist:5
      "🟡 Audio playback transport (play/pause/seek) on the preview player
       behaves the same for English as before the change."

``ShareOptionsSheet.tsx`` builds exactly three options — PDF, "PDF & Audio
Walkthrough" (flag-gated) and "Print or Download". There is no Text
option. ``DockedAudioPlayer.tsx`` renders its progress bar as nested plain
``View``s with ``accessibilityRole="progressbar"``; there is no Pressable,
no gesture handler and no seek. Both lines were marked PASS only because
the runner used judgment and graded what actually ships.

They reached a tester because the four critics that existed all iterate
exactly ``happy_path`` / ``edge_cases`` / ``integration_tests``, so no
pass had ever read ``regression_checklist``.

Two things this file pins, in roughly equal measure:

  * the critic narrows the phantom lines (``test_narrows_*``), and
  * **it leaves everything else alone** (``test_grounded_*``,
    ``test_unverifiable_*``, ``test_invariants_*``). The regression
    checklist's value is breadth — it catches behaviour adjacent to the
    change — so a critic that trimmed the checklist down to what it could
    prove would be a worse bug than the one it fixes. The no-op tests are
    the guard against that, and they are the ones that should fail if
    someone later makes this pass aggressive.
"""

import pytest

from src.app.models import TestPlan
from src.app.regression_grounding_critic import (
    apply_regression_verdicts,
    build_regression_critic_user_message,
    build_regression_grounding_inputs,
    build_search_query,
    line_id,
    names_a_control,
    parse_regression_verdicts,
    split_priority,
)
from src.app.services.progress_key import fingerprint


# ─── fixtures ─────────────────────────────────────────────────────────────────

#: Plan 522's regression checklist, verbatim, in order. Indices 4 and 5 are
#: the two ungradeable lines; the other five are correct and must survive
#: every pass untouched.
PLAN_522_CHECKLIST = [
    "🔴 Email share without the audio walkthrough (plain 'PDF' option) still "
    "reaches the Prepare Message screen and sends successfully.",
    "🔴 With isAudioLanguageSelectionEnabled OFF, the share preview audio "
    "walkthrough still plays English end-to-end exactly as before — no "
    "language control, no transcript regression.",
    "🔴 The Prepare Message screen still pre-populates the subject and body, "
    "accepts a client first name in share-flow-client-name-input, and sends "
    "via share-flow-send-button.",
    "🟡 Preview settings sheet still opens and its Change Template row still "
    "navigates to template selection when the PDF-templates flag is on (the "
    "language section was intentionally removed from this sheet).",
    "🟡 Text, Print and Download share options from the share menu still work "
    "and are unaffected by the audio-language change.",
    "🟡 Audio playback transport (play/pause/seek) on the preview player "
    "behaves the same for English as before the change.",
    "🟢 Navigating back from the preview screen and re-entering restores a "
    "playable walkthrough without duplicate document preparation.",
]

#: The real source that contradicts line 4 — the option array is the
#: complete list, and "Text" is not a member of it.
SHARE_OPTIONS_SOURCE = {
    "path": "apps/expo/src/components/share/ShareOptionsSheet.tsx",
    "ref": "main",
    "content": """
  const shareOptions = useMemo<SelectOption[]>(
    () => [
      { key: "pdf", label: "PDF" },
      ...(isAudioWalkthroughEnabled
        ? [{ key: "pdf-audio", label: "PDF & Audio Walkthrough" }]
        : []),
      { key: "print-download", label: "Print or Download" },
    ],
    [isAudioWalkthroughEnabled],
  );
""",
}

#: The real source that contradicts line 5 — play/pause is a Pressable,
#: the progress bar is not.
AUDIO_PLAYER_SOURCE = {
    "path": "apps/expo/src/components/share-flow/DockedAudioPlayer.tsx",
    "ref": "main",
    "content": """
        <Pressable
          onPress={isPlaying ? pause : onRequestPlay}
          accessibilityLabel={isPlaying ? "Pause" : "Play"}
          testID={isLoading ? `${testID}-spinner` : `${testID}-play-pause-button`}
        >
        </Pressable>
          <View
            className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/20"
            accessibilityRole="progressbar"
            accessibilityValue={{ min: 0, max: 1, now: elapsedFraction }}
          >
            <View className="h-full rounded-full bg-white" style={{ width: elapsedWidth }} />
          </View>
          <Text className="text-xs text-white/70">{remainingLabel}</Text>
""",
}


def _plan_522() -> TestPlan:
    return TestPlan(
        happy_path=[],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=list(PLAN_522_CHECKLIST),
    )


def _narrow(index: int, rewrite: str, reason: str = "contradicted by source") -> dict:
    return {
        line_id(index): {
            "verdict": "unfounded_control",
            "reason": reason,
            "rewrite": rewrite,
        }
    }


# ─── split_priority ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("emoji", ["🔴", "🟡", "🟢"])
def test_split_priority_separates_each_emoji_from_the_body(emoji):
    prefix, body = split_priority(f"{emoji} Something still works.")
    assert prefix == f"{emoji} "
    assert body == "Something still works."


def test_split_priority_leaves_an_unprefixed_line_whole():
    assert split_priority("Something still works.") == ("", "Something still works.")


def test_split_priority_round_trips():
    """A rewrite is reassembled as prefix + new body, so the split has to be
    lossless for the unchanged half — the emoji drives the frontend's
    severity styling and is not the critic's to reassign."""
    for line in PLAN_522_CHECKLIST:
        prefix, body = split_priority(line)
        assert prefix + body == line


# ─── names_a_control: which lines are even candidates ─────────────────────────


def test_the_two_flawed_lines_are_candidates():
    assert names_a_control(PLAN_522_CHECKLIST[4])  # "share options ... share menu"
    assert names_a_control(PLAN_522_CHECKLIST[5])  # "(play/pause/seek)"


def test_a_flow_only_line_is_never_a_candidate():
    """This is the breadth guarantee. Line 6 asserts a *flow* — navigate
    back, re-enter, get a playable walkthrough — and names no control. It
    must never reach the LLM, because adjacency coverage lives in lines
    shaped like this one and the critic has no business touching them."""
    assert not names_a_control(PLAN_522_CHECKLIST[6])


def test_a_behaviour_line_with_hyphenated_english_is_not_a_candidate():
    """"plays English end-to-end" must not trip the testID-token matcher —
    that's why the token pattern requires three or more segments."""
    assert not names_a_control(
        "🔴 The audio walkthrough still plays English end-to-end exactly as before."
    )


def test_a_line_naming_a_real_testid_is_a_candidate():
    assert names_a_control(PLAN_522_CHECKLIST[2])  # share-flow-send-button


def test_an_empty_or_emoji_only_line_is_not_a_candidate():
    assert not names_a_control("")
    assert not names_a_control("🟡 ")


# ─── build_regression_grounding_inputs ────────────────────────────────────────


def test_only_control_naming_lines_are_sent_to_the_llm():
    plan = _plan_522()
    got = build_regression_grounding_inputs(plan, hits_by_line={})
    sent = {item["line_id"] for item in got}
    # Candidates: 0 ("plain 'PDF' option"), 1 ("no language control"),
    # 2 (two testIDs), 3 ("settings sheet" / "Change Template row"), and the
    # two flawed lines 4 and 5. Being a candidate costs nothing — a line
    # whose controls are real comes back `grounded` and is left alone.
    assert {line_id(i) for i in (0, 1, 2, 3, 4, 5)} <= sent
    # 6 asserts a flow and names no control, so it is never examined at all.
    assert line_id(6) not in sent


def test_the_priority_emoji_is_stripped_from_the_line_the_llm_sees():
    plan = _plan_522()
    got = {i["line_id"]: i for i in build_regression_grounding_inputs(plan, {})}
    item = got[line_id(4)]
    assert item["priority"] == "🟡"
    assert not item["line"].startswith("🟡")
    assert item["line"].startswith("Text, Print and Download")


def test_a_line_with_no_snippets_is_still_sent():
    """Unlike the code-grounding critic, a line with zero source hits is
    NOT dropped from the pass. "We looked and found nothing" and "we never
    looked" are different facts, and only by sending the line can the
    critic return `unverifiable` for it — dropping it silently would leave
    the plan claiming a check that never happened."""
    plan = _plan_522()
    got = build_regression_grounding_inputs(plan, hits_by_line={})
    assert got
    assert all(item["source_snippets"] == [] for item in got)


def test_snippets_are_attached_to_the_right_line():
    plan = _plan_522()
    got = {
        i["line_id"]: i
        for i in build_regression_grounding_inputs(
            plan, hits_by_line={4: [SHARE_OPTIONS_SOURCE], 5: [AUDIO_PLAYER_SOURCE]}
        )
    }
    assert got[line_id(4)]["source_snippets"][0]["path"].endswith("ShareOptionsSheet.tsx")
    assert got[line_id(5)]["source_snippets"][0]["path"].endswith("DockedAudioPlayer.tsx")
    assert got[line_id(2)]["source_snippets"] == []


def test_the_payload_is_capped():
    plan = TestPlan(
        happy_path=[], edge_cases=[], integration_tests=[],
        regression_checklist=[f"🟡 The button {i} still works." for i in range(40)],
    )
    assert len(build_regression_grounding_inputs(plan, {}, limit=12)) == 12


# ─── the user message ─────────────────────────────────────────────────────────


def test_the_user_message_says_the_testid_reference_is_incomplete():
    """The generated reference misses testIDs written as conditional
    expressions, so the model must not read absence from it as proof. The
    prompt says so explicitly; if that sentence is ever dropped, a rewrite
    could delete a real control on the strength of a file that never
    listed it."""
    plan = _plan_522()
    lines = build_regression_grounding_inputs(
        plan, {}, testid_reference="### DockedAudioPlayer\n- `{dynamic}-language-button`"
    )
    msg = build_regression_critic_user_message(lines)
    assert "Absence proves nothing" in msg
    assert "DockedAudioPlayer" in msg


def test_the_user_message_marks_lines_with_no_source_retrieved():
    plan = _plan_522()
    msg = build_regression_critic_user_message(
        build_regression_grounding_inputs(plan, {})
    )
    assert "none retrieved for this line" in msg


def test_the_user_message_is_deterministic():
    plan = _plan_522()
    lines = build_regression_grounding_inputs(plan, {4: [SHARE_OPTIONS_SOURCE]})
    assert build_regression_critic_user_message(lines) == (
        build_regression_critic_user_message(lines)
    )


# ─── build_search_query ───────────────────────────────────────────────────────


def test_the_search_query_drops_the_emoji_and_the_control_nouns():
    """The control noun is what made the line a candidate; it is also in
    every file of a UI repo, so leaving it in buries the domain terms that
    actually locate the source."""
    q = build_search_query(PLAN_522_CHECKLIST[4])
    assert "🟡" not in q
    assert "options" not in q.split() and "menu" not in q.split()
    assert "share" in q.split()


def test_the_search_query_drops_regression_boilerplate():
    q = build_search_query("🟡 The share sheet still works exactly the same as before.")
    for noise in ("still", "works", "same", "before", "the"):
        assert noise not in q.split()


# ─── parse_regression_verdicts ────────────────────────────────────────────────


def test_parses_each_verdict():
    got = parse_regression_verdicts({
        "verdicts": [
            {"line_id": line_id(0), "verdict": "grounded", "reason": "found it"},
            {"line_id": line_id(4), "verdict": "unfounded_control",
             "reason": "no Text option", "rewrite": "The Print or Download option still works."},
            {"line_id": line_id(5), "verdict": "unverifiable", "reason": "no source"},
        ]
    })
    assert got[line_id(0)]["verdict"] == "grounded"
    assert got[line_id(4)]["rewrite"] == "The Print or Download option still works."
    assert got[line_id(5)]["verdict"] == "unverifiable"


def test_an_unfounded_verdict_without_a_rewrite_becomes_unverifiable():
    """The critic's only action for an unfounded line is to substitute the
    rewrite. With no rewrite there is nothing to apply, and the obvious
    alternative — deleting the line — is precisely the over-correction
    this pass exists not to do. So it degrades to "left as written"."""
    for bad in ({}, {"rewrite": ""}, {"rewrite": "   "}, {"rewrite": None}):
        got = parse_regression_verdicts({
            "verdicts": [{"line_id": line_id(4), "verdict": "unfounded_control",
                          "reason": "r", **bad}]
        })
        assert got[line_id(4)]["verdict"] == "unverifiable"
        assert got[line_id(4)]["rewrite"] == ""


def test_malformed_entries_are_dropped_not_raised():
    got = parse_regression_verdicts({
        "verdicts": [
            "not a dict",
            {"verdict": "grounded"},                       # no line_id
            {"line_id": "", "verdict": "grounded"},        # blank line_id
            {"line_id": line_id(1), "verdict": "maybe"},   # bad verdict
            {"line_id": line_id(2), "verdict": "grounded", "reason": "ok"},
        ]
    })
    assert list(got) == [line_id(2)]


def test_junk_input_returns_an_empty_dict():
    for junk in (None, [], {}, "nope", 7, {"verdicts": "nope"}):
        assert parse_regression_verdicts(junk) == {}


# ─── apply: the narrowing half ────────────────────────────────────────────────


def test_narrows_the_phantom_text_share_option():
    """Line 4's fix keeps the surface (the share menu, the sibling option
    beside PDF & Audio Walkthrough) and drops only "Text"."""
    plan = _plan_522()
    rewrite = (
        "The Print or Download share option still works from the share menu and "
        "is unaffected by the audio-language change."
    )
    notes = apply_regression_verdicts(plan, _narrow(4, rewrite))

    assert plan.regression_checklist[4] == f"🟡 {rewrite}"
    assert "Text" not in plan.regression_checklist[4]
    assert "Print or Download" in plan.regression_checklist[4]
    assert "share menu" in plan.regression_checklist[4]
    assert notes[0]["status"] == "narrowed"
    assert notes[0]["line_index"] == 4
    assert notes[0]["original"].startswith("Text, Print and Download")


def test_narrows_the_phantom_seek_control():
    """Line 5's fix drops seek and substitutes observables the source does
    show (the progress bar, the remaining-time label) so the line keeps its
    grading power instead of getting thinner."""
    plan = _plan_522()
    rewrite = (
        "Audio playback transport (play/pause) on the preview player behaves the "
        "same for English as before the change — the progress bar advances and "
        "the remaining-time label counts down."
    )
    apply_regression_verdicts(plan, _narrow(5, rewrite))

    assert "seek" not in plan.regression_checklist[5]
    assert "play/pause" in plan.regression_checklist[5]
    assert "progress bar" in plan.regression_checklist[5]


def test_the_priority_emoji_survives_a_rewrite():
    plan = _plan_522()
    apply_regression_verdicts(plan, _narrow(4, "A narrowed line."))
    assert plan.regression_checklist[4] == "🟡 A narrowed line."


def test_a_hand_built_unfounded_verdict_with_no_rewrite_is_a_no_op():
    """parse_* downgrades these, so reaching apply_* means a caller built
    the dict by hand. The line must still survive."""
    plan = _plan_522()
    before = list(plan.regression_checklist)
    notes = apply_regression_verdicts(plan, {
        line_id(4): {"verdict": "unfounded_control", "reason": "r", "rewrite": ""}
    })
    assert plan.regression_checklist == before
    assert notes == []


# ─── apply: the no-op half — the guard against over-correcting ────────────────


def test_grounded_lines_are_never_touched():
    plan = _plan_522()
    before = list(plan.regression_checklist)
    notes = apply_regression_verdicts(plan, {
        line_id(i): {"verdict": "grounded", "reason": "found it", "rewrite": ""}
        for i in range(len(before))
    })
    assert plan.regression_checklist == before
    assert notes == []
    assert not (plan.regression_grounding_notes or [])


def test_the_five_correct_plan_522_lines_survive_a_full_pass():
    """The load-bearing regression test for over-correction. The real plan
    522 verdicts: two narrowed, the rest grounded or unverifiable. Every
    line the critic was not asked to narrow must come out byte-identical —
    including the flow-shaped line 6 that carries adjacency coverage."""
    plan = _plan_522()
    verdicts = {
        line_id(0): {"verdict": "grounded", "reason": "ShareOptionsSheet builds 'PDF'", "rewrite": ""},
        line_id(1): {"verdict": "grounded", "reason": "flag read in ShareFlowProvider", "rewrite": ""},
        line_id(2): {"verdict": "grounded", "reason": "both testIDs in the diff", "rewrite": ""},
        line_id(3): {"verdict": "grounded", "reason": "preview-settings-change-template-row", "rewrite": ""},
        **_narrow(4, "The Print or Download share option still works."),
        **_narrow(5, "Audio playback transport (play/pause) behaves the same."),
        line_id(6): {"verdict": "unverifiable", "reason": "no navigation source", "rewrite": ""},
    }
    apply_regression_verdicts(plan, verdicts)

    for i in (0, 1, 2, 3, 6):
        assert plan.regression_checklist[i] == PLAN_522_CHECKLIST[i], (
            f"line {i} was modified; the critic may only narrow lines it was "
            f"asked to narrow"
        )


def test_unverifiable_never_changes_the_text():
    """Failure to prove is not grounds to weaken a line. An unverifiable
    verdict annotates and nothing more."""
    plan = _plan_522()
    before = list(plan.regression_checklist)
    notes = apply_regression_verdicts(plan, {
        line_id(4): {"verdict": "unverifiable", "reason": "no snippets", "rewrite": ""},
    })
    assert plan.regression_checklist == before
    assert notes[0]["status"] == "unchecked"
    assert notes[0]["rewritten"] == ""
    assert "no snippets" in notes[0]["explanation"]


def test_a_verdict_for_a_non_candidate_line_is_ignored():
    """Even if the model hallucinates a verdict for the flow-shaped line,
    it was never a candidate and cannot be rewritten."""
    plan = _plan_522()
    apply_regression_verdicts(plan, _narrow(6, "Something much vaguer."))
    assert plan.regression_checklist[6] == PLAN_522_CHECKLIST[6]


def test_no_verdicts_is_a_no_op():
    plan = _plan_522()
    before = list(plan.regression_checklist)
    assert apply_regression_verdicts(plan, {}) == []
    assert plan.regression_checklist == before


# ─── invariants: the section count and the progress key ───────────────────────


@pytest.mark.parametrize("verdict,extra", [
    ("grounded", {}),
    ("unverifiable", {}),
    ("unfounded_control", {"rewrite": "A much shorter line."}),
])
def test_the_line_count_is_invariant_under_every_verdict(verdict, extra):
    plan = _plan_522()
    apply_regression_verdicts(plan, {
        line_id(i): {"verdict": verdict, "reason": "r", "rewrite": "", **extra}
        for i in range(len(PLAN_522_CHECKLIST))
    })
    assert len(plan.regression_checklist) == len(PLAN_522_CHECKLIST)


def test_the_progress_key_fingerprint_survives_the_pass():
    """Plan 522's key is SK-2342:1-5-0-7 with nine cases already marked
    passed. The fingerprint counts section *sizes*, so rewording is safe
    and adding, removing or splitting a line is not — this pins that the
    critic only ever does the former."""
    body = {
        "happy_path": [{"title": "h"}],
        "edge_cases": [{"title": f"e{i}"} for i in range(5)],
        "integration_tests": [],
        "regression_checklist": list(PLAN_522_CHECKLIST),
    }
    assert fingerprint(body) == "1-5-0-7"

    plan = TestPlan(
        happy_path=body["happy_path"], edge_cases=body["edge_cases"],
        integration_tests=[], regression_checklist=body["regression_checklist"],
    )
    apply_regression_verdicts(plan, {
        **_narrow(4, "The Print or Download share option still works."),
        **_narrow(5, "Audio playback transport (play/pause) behaves the same."),
    })
    body["regression_checklist"] = plan.regression_checklist
    assert fingerprint(body) == "1-5-0-7"


def test_notes_accumulate_rather_than_replace():
    plan = _plan_522()
    plan.regression_grounding_notes = [{"line_index": 99, "status": "unchecked"}]
    apply_regression_verdicts(plan, _narrow(4, "A narrowed line."))
    assert len(plan.regression_grounding_notes) == 2
    assert plan.regression_grounding_notes[0]["line_index"] == 99


def test_a_plan_with_no_checklist_is_handled():
    plan = TestPlan(happy_path=[], edge_cases=[], regression_checklist=[])
    assert apply_regression_verdicts(plan, _narrow(0, "x")) == []
    assert build_regression_grounding_inputs(plan, {}) == []
