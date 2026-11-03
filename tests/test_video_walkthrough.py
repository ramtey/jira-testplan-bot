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

from src.app.services import progress_key
from src.app.video_walkthrough import (
    DEFAULT_STEP_BUDGET,
    build_walkthrough,
    validate_chapters,
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


def test_stored_walkthrough_is_ignored_in_favour_of_the_current_cases():
    """The stored copy is a snapshot. Serving it verbatim is how plan 655 kept
    its bad ids, so the body's current cases always win."""
    body = {
        "happy_path": [case("now", 2)],
        "video_walkthrough": {
            "chapters": [{"case_id": "happy_path:7", "title": "gone", "steps": 9}]
        },
    }
    wt = walkthrough_from_plan_body(body)
    assert [(c["case_id"], c["title"]) for c in wt["chapters"]] == [("happy_path:0", "now")]


# ---- chapter ids are the plan's canonical ids ------------------------------


def assert_chapters_match_plan(body):
    """Every chapter names a case that exists in this body, under that id."""
    index = progress_key.case_index(body)
    for chapter in walkthrough_from_plan_body(body)["chapters"]:
        assert chapter["case_id"] in index, chapter["case_id"]
        assert index[chapter["case_id"]]["title"] == chapter["title"]


def test_plan_655_shape_covered_case_does_not_shift_later_ids():
    """SK-2948 / plan 655: happy_path[3] was flagged covered, so it lives at
    covered_by_unit_test:0 and the cases after it are happy_path:3 and :4. The
    raw-position numbering produced happy_path:4 (the wrong case) and
    happy_path:5 (no such case)."""
    body = {
        "happy_path": [
            case("Utah", 6),
            case("Utah API", 4, surface="backend_http"),
            case("WA", 6),
            case("Indiana", 3, covered_by_unit_test=True),
            case("County-only", 5),
            case("Same Utah twice", 4),
        ]
    }
    wt = walkthrough_from_plan_body(body)
    assert [(c["case_id"], c["title"]) for c in wt["chapters"]] == [
        ("happy_path:0", "Utah"),
        ("happy_path:2", "WA"),
        ("happy_path:3", "County-only"),
        ("happy_path:4", "Same Utah twice"),
    ]
    omitted = {o["title"]: o["case_id"] for o in wt["omitted"]}
    assert omitted == {"Utah API": "happy_path:1", "Indiana": "covered_by_unit_test:0"}
    assert_chapters_match_plan(body)


def test_chapters_follow_a_case_moved_to_another_section():
    """Move a case out of happy_path, once by flagging it covered and once by
    moving it to edge_cases. Either way the chapters must follow: the moved
    case drops out, and every later chapter takes its new id."""
    happy = [case("a", 2), case("b", 2), case("c", 2), case("d", 2)]
    before = walkthrough_from_plan_body({"happy_path": happy})
    assert [c["case_id"] for c in before["chapters"]] == [
        "happy_path:0", "happy_path:1", "happy_path:2", "happy_path:3",
    ]

    flagged = {"happy_path": [dict(c) for c in happy]}
    flagged["happy_path"][1]["covered_by_unit_test"] = True
    moved = {"happy_path": [happy[0], happy[2], happy[3]], "edge_cases": [happy[1]]}

    for body in (flagged, moved):
        # Even with the old walkthrough still stored in the body.
        body["video_walkthrough"] = before
        wt = walkthrough_from_plan_body(body)
        assert [(c["case_id"], c["title"]) for c in wt["chapters"]] == [
            ("happy_path:0", "a"),
            ("happy_path:1", "c"),
            ("happy_path:2", "d"),
        ]
        assert "b" not in [c["title"] for c in wt["chapters"]]
        assert_chapters_match_plan(body)


def test_build_from_section_alone_matches_the_whole_plan():
    """Callers holding only happy_path get ids that hold in the full plan:
    happy_path is first in both of progress_key's orderings."""
    happy = [case("x", 1, covered_by_unit_test=True), case("y", 2)]
    body = {
        "happy_path": happy,
        "edge_cases": [case("e", 1, covered_by_unit_test=True)],
    }
    assert build_walkthrough(happy)["chapters"] == walkthrough_from_plan_body(body)["chapters"]
    assert build_walkthrough(happy)["omitted"][0]["case_id"] == "covered_by_unit_test:0"


# ---- the safeguard ---------------------------------------------------------


def test_validation_drops_missing_and_mismatched_chapters():
    body = {"happy_path": [case("a", 2), case("b", 3, preconditions="P2")]}
    stale = {
        "chapters": [
            {"case_id": "happy_path:0", "title": "a", "steps": 2,
             "preconditions": None, "starts_chapter": True},
            # Exists, but is a different case: the id-only check misses this.
            {"case_id": "happy_path:1", "title": "not b", "steps": 3,
             "preconditions": "P2", "starts_chapter": True},
            # Does not exist at all.
            {"case_id": "happy_path:5", "title": "ghost", "steps": 4,
             "preconditions": None, "starts_chapter": True},
            # Exists, but not in happy_path.
            {"case_id": "edge_cases:0", "title": "a", "steps": 1,
             "preconditions": None, "starts_chapter": True},
        ],
        "omitted": [],
    }
    wt = validate_chapters(stale, body)
    assert [c["case_id"] for c in wt["chapters"]] == ["happy_path:0"]
    assert [o["case_id"] for o in wt["omitted"]] == [
        "happy_path:1", "happy_path:5", "edge_cases:0",
    ]
    assert {o["reason"] for o in wt["omitted"]} == {"stale_case_id"}
    assert wt["selected_steps"] == 2
    assert wt["estimated_seconds"] == 5
    # The input is untouched.
    assert len(stale["chapters"]) == 4


def test_validation_reopens_a_chapter_when_its_opener_was_dropped():
    body = {"happy_path": [case("a", 1, preconditions="P1"), case("b", 1, preconditions="P2")]}
    stale = {
        "chapters": [
            {"case_id": "happy_path:9", "title": "ghost", "steps": 1,
             "preconditions": "P2", "starts_chapter": True},
            {"case_id": "happy_path:1", "title": "b", "steps": 1,
             "preconditions": "P2", "starts_chapter": False},
        ],
    }
    wt = validate_chapters(stale, body)
    assert [(c["case_id"], c["starts_chapter"]) for c in wt["chapters"]] == [
        ("happy_path:1", True),
    ]


def test_validation_leaves_a_clean_walkthrough_alone():
    body = {"happy_path": [case("a", 2)]}
    wt = build_walkthrough(body["happy_path"])
    assert validate_chapters(wt, body) is wt


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
