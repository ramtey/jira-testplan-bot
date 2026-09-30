"""The happy-path screen recording plan, and the two things it must not get wrong.

The PM watches the video and does not read the plan, so "record the happy path"
is the deliverable. Two failures make that recording useless, and both are
pinned here:

1. **Length.** A happy_path section runs to 34 cases and 173 steps in the tail.
   Capping by case count does not bound video length because cases run 1-16
   steps; capping by step budget does. The worst case has to stay inside a
   couple of minutes without ever cutting a case in half.

2. **Chapters.** The section is not one continuous take — only 47 of 297 plans
   share one precondition across every happy case. A recording that ignores
   that shows unexplained state resets. Every precondition change has to
   surface as a chapter boundary so the ledger can timestamp it.

The third property, quieter but the reason the module exists at all: there is
exactly one producer. The browser used to derive this list itself, so the UAT
runner — a separate agent on the other side of the API — could not see it.
"""

from src.app.video_walkthrough import (
    DEFAULT_STEP_BUDGET,
    build_walkthrough,
    walkthrough_from_plan_body,
)


def case(title, steps, *, preconditions=None, **extra):
    c = {"title": title, "steps": [f"step {i}" for i in range(steps)]}
    if preconditions is not None:
        c["preconditions"] = preconditions
    c.update(extra)
    return c


# ---- selection order -------------------------------------------------------


def test_takes_cases_in_plan_order_without_resorting():
    """Plan order is already priority order — happy_path[0] is `critical` in
    95.3% of stored plans. Re-sorting would only invent a disagreement."""
    wt = build_walkthrough(
        [
            case("first", 2, priority="high"),
            case("second", 2, priority="critical"),
        ]
    )
    assert [c["title"] for c in wt["chapters"]] == ["first", "second"]
    assert [c["case_id"] for c in wt["chapters"]] == [
        "happy_path:0",
        "happy_path:1",
    ]


def test_case_ids_index_the_raw_section_including_skipped_cases():
    """An omitted case still consumes its index. The id has to address the
    case in the stored plan, not its position among the filmable ones."""
    wt = build_walkthrough(
        [
            case("backend thing", 3, surface="backend_http"),
            case("the visible flow", 3),
        ]
    )
    assert [c["case_id"] for c in wt["chapters"]] == ["happy_path:1"]


# ---- the step budget -------------------------------------------------------


def test_budget_cuts_on_whole_cases_only():
    """Half a case on film is not a shorter video, it is one that stops
    mid-flow with nothing on screen saying so."""
    wt = build_walkthrough(
        [case("a", 20), case("b", 15), case("c", 20)], budget=40
    )
    assert [c["title"] for c in wt["chapters"]] == ["a", "b"]
    assert wt["selected_steps"] == 35
    assert wt["truncated"] is True
    assert [o["reason"] for o in wt["omitted"]] == ["over_budget"]


def test_a_single_oversized_case_is_still_recorded():
    """Returning an empty plan because the one case is long would leave the
    ticket with no video at all, which is worse than a long one."""
    wt = build_walkthrough([case("the whole flow", 60)], budget=40)
    assert [c["title"] for c in wt["chapters"]] == ["the whole flow"]
    assert wt["selected_steps"] == 60
    assert wt["truncated"] is False


def test_later_small_case_is_not_squeezed_in_past_a_dropped_one():
    """Once the budget is blown the recording stops there. Skipping ahead to
    whatever still fits would film the flows out of order."""
    wt = build_walkthrough(
        [case("a", 30), case("big", 30), case("tiny", 2)], budget=40
    )
    assert [c["title"] for c in wt["chapters"]] == ["a"]
    assert {o["title"] for o in wt["omitted"]} == {"big", "tiny"}


def test_budget_defaults_and_rejects_nonsense():
    assert build_walkthrough([case("a", 1)], budget=0)["budget"] == DEFAULT_STEP_BUDGET
    assert build_walkthrough([case("a", 1)], budget=-5)["budget"] == DEFAULT_STEP_BUDGET
    assert build_walkthrough([case("a", 1)])["budget"] == DEFAULT_STEP_BUDGET


def test_total_steps_counts_the_whole_filmable_section_not_just_what_fits():
    """`total_steps` is what the tester would see if there were no budget —
    it is how the caller knows how much was left on the floor."""
    wt = build_walkthrough([case("a", 30), case("b", 30)], budget=40)
    assert wt["selected_steps"] == 30
    assert wt["total_steps"] == 60


# ---- chapters --------------------------------------------------------------


def test_precondition_change_starts_a_chapter():
    wt = build_walkthrough(
        [
            case("a", 2, preconditions="Logged in as agent"),
            case("b", 2, preconditions="Logged in as agent"),
            case("c", 2, preconditions="Logged in as broker"),
        ]
    )
    assert [c["starts_chapter"] for c in wt["chapters"]] == [True, False, True]


def test_first_chapter_always_starts_one_even_with_no_precondition():
    wt = build_walkthrough([case("a", 2), case("b", 2)])
    assert [c["starts_chapter"] for c in wt["chapters"]] == [True, False]


def test_shared_precondition_across_every_case_is_one_chapter():
    """47 of 297 plans look like this — the continuous-take case, which is
    the minority and must not be assumed."""
    wt = build_walkthrough(
        [case(t, 2, preconditions="Same setup") for t in ("a", "b", "c")]
    )
    assert [c["starts_chapter"] for c in wt["chapters"]] == [True, False, False]


def test_blank_and_missing_preconditions_are_the_same_chapter():
    """A case that omits the field and one that sets it to whitespace describe
    the same setup; treating them as different would invent a state reset."""
    wt = build_walkthrough(
        [case("a", 2, preconditions="  "), case("b", 2)]
    )
    assert [c["starts_chapter"] for c in wt["chapters"]] == [True, False]
    assert wt["chapters"][0]["preconditions"] is None


# ---- what cannot be filmed -------------------------------------------------


def test_unfilmable_surfaces_are_omitted_with_a_reason():
    wt = build_walkthrough(
        [
            case("http", 3, surface="backend_http"),
            case("terminal", 3, surface="cli"),
            case("by hand", 3, surface="manual_only"),
            case("on screen", 3, surface="web_ui"),
        ]
    )
    assert [c["title"] for c in wt["chapters"]] == ["on screen"]
    assert {o["reason"] for o in wt["omitted"]} == {
        "surface:backend_http",
        "surface:cli",
        "surface:manual_only",
    }


def test_unit_covered_cases_are_omitted():
    """The planner flagged them so they would leave the manual checklist. A
    video of a case the runner never performs is a video of something else."""
    wt = build_walkthrough(
        [case("covered", 3, covered_by_unit_test=True), case("driven", 3)]
    )
    assert [c["title"] for c in wt["chapters"]] == ["driven"]
    assert wt["omitted"][0]["reason"] == "covered_by_unit_test"


def test_surface_matching_is_case_insensitive():
    wt = build_walkthrough([case("http", 3, surface="Backend_HTTP")])
    assert wt["chapters"] == []


def test_unset_surface_is_filmable():
    """`surface` is only populated from plan id ~400 onward. Treating absent
    as unfilmable would blank the walkthrough for the whole back catalogue."""
    wt = build_walkthrough([case("older plan case", 3)])
    assert len(wt["chapters"]) == 1


def test_everything_unfilmable_returns_no_chapters_rather_than_pretending():
    wt = build_walkthrough(
        [case("a", 3, surface="backend_http"), case("b", 3, surface="cli")]
    )
    assert wt["chapters"] == []
    assert wt["selected_steps"] == 0
    assert len(wt["omitted"]) == 2


# ---- degenerate input ------------------------------------------------------


def test_missing_or_malformed_sections_do_not_raise():
    for bad in (None, {}, "happy_path", 7, []):
        wt = build_walkthrough(bad)
        assert wt["chapters"] == []
        assert wt["selected_steps"] == 0


def test_non_dict_entries_are_skipped_without_consuming_a_chapter():
    wt = build_walkthrough(["a bare string", case("real", 2)])
    assert [c["case_id"] for c in wt["chapters"]] == ["happy_path:1"]


def test_case_with_no_steps_is_omitted():
    wt = build_walkthrough([{"title": "stepless"}, case("real", 2)])
    assert [c["title"] for c in wt["chapters"]] == ["real"]
    assert wt["omitted"][0]["reason"] == "no_steps"


def test_untitled_case_still_gets_an_addressable_label():
    wt = build_walkthrough([{"steps": ["a", "b"]}])
    assert wt["chapters"][0]["title"] == "(untitled case 0)"
    assert wt["chapters"][0]["case_id"] == "happy_path:0"


# ---- reading it back off a stored plan -------------------------------------


def test_stored_walkthrough_is_returned_as_is():
    body = {"video_walkthrough": {"chapters": [{"case_id": "happy_path:0"}]}}
    assert walkthrough_from_plan_body(body)["chapters"][0]["case_id"] == "happy_path:0"


def test_older_plan_without_one_is_recomputed_from_happy_path():
    """The back catalogue predates this field; the endpoint must still answer."""
    body = {"happy_path": [case("legacy", 4)]}
    wt = walkthrough_from_plan_body(body)
    assert [c["title"] for c in wt["chapters"]] == ["legacy"]
    assert wt["selected_steps"] == 4


def test_malformed_stored_walkthrough_falls_back_to_recomputing():
    body = {"video_walkthrough": {"chapters": "not a list"}, "happy_path": [case("x", 2)]}
    assert [c["title"] for c in walkthrough_from_plan_body(body)["chapters"]] == ["x"]


def test_non_dict_body_is_survivable():
    assert walkthrough_from_plan_body("oops")["chapters"] == []


# ---- the shape the callers rely on -----------------------------------------


def test_estimated_seconds_tracks_the_selected_steps():
    wt = build_walkthrough([case("a", 20)], budget=40)
    assert wt["estimated_seconds"] == 50


def test_realistic_median_plan_stays_inside_two_minutes():
    """Median stored plan: 5 cases, 21 steps. This is the shape the rule is
    tuned for, and it must not need truncating."""
    wt = build_walkthrough(
        [case(f"case {i}", n) for i, n in enumerate([5, 4, 4, 4, 4])]
    )
    assert wt["truncated"] is False
    assert wt["selected_steps"] == 21
    assert wt["estimated_seconds"] <= 120
