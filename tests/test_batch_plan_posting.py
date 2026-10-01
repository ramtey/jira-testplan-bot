"""A batch plan sits beside each ticket's own plan, not on top of it.

Before this, one plan comment slot was shared: a plan covering seven tickets
was posted to all seven and reused whatever plan comment it found, so it
replaced the plan written for each ticket alone. These tests hold the two
halves of the fix — separate comment slots, and one host ticket carrying the
batch plan while the rest carry a pointer to it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.jira_client import (
    BATCH_POINTER_MARKER,
    BATCH_TEST_PLAN_MARKER,
    TEST_PLAN_MARKER,
    JiraClient,
    PlanCommentKind,
    _comment_marker_kind,
    _is_legacy_batch_comment,
    carries_bot_plan_marker,
)
from src.app.services.batch_plan_host import (
    BatchHostCandidate,
    choose_batch_host,
)
from src.app.services import plan_posting


def _comment(comment_id: str, first_line: str, body_extra: str = "") -> dict:
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": first_line}]}
    ]
    if body_extra:
        content.append(
            {"type": "paragraph", "content": [{"type": "text", "text": body_extra}]}
        )
    return {"id": comment_id, "body": {"type": "doc", "content": content}}


# ---------------------------------------------------------------------------
# The property the whole split rests on
# ---------------------------------------------------------------------------


def test_no_marker_is_a_substring_of_another():
    """Comment matching is a substring test, so one marker containing another
    would make a post of one kind reuse the other kind's comments — exactly the
    overwrite this module exists to prevent."""
    markers = [TEST_PLAN_MARKER, BATCH_TEST_PLAN_MARKER, BATCH_POINTER_MARKER]
    for a in markers:
        for b in markers:
            if a is not b:
                assert a not in b, f"{a!r} is a substring of {b!r}"


def test_each_marker_classifies_as_its_own_kind():
    assert _comment_marker_kind(_comment("1", TEST_PLAN_MARKER)) is PlanCommentKind.single
    assert _comment_marker_kind(_comment("2", BATCH_TEST_PLAN_MARKER)) is PlanCommentKind.batch
    assert _comment_marker_kind(_comment("3", BATCH_POINTER_MARKER)) is PlanCommentKind.pointer
    assert _comment_marker_kind(_comment("4", "just a human comment")) is None


def test_a_versioned_batch_marker_is_still_a_batch_comment():
    """The version note rides on the marker line, so classification has to
    survive it — otherwise every re-post would create a second comment."""
    versioned = f"{BATCH_TEST_PLAN_MARKER} — v3 · updated in place 23 Sep 2026"
    assert _comment_marker_kind(_comment("1", versioned)) is PlanCommentKind.batch


def test_bounce_detection_ignores_every_bot_marker():
    """The bot must never be handed its own output as the reason a ticket
    bounced. That check tested the single-ticket marker alone, so a batch plan
    or a pointer would have been quoted straight back into the next prompt."""
    assert carries_bot_plan_marker(f"{TEST_PLAN_MARKER}\n\nbody")
    assert carries_bot_plan_marker(f"{BATCH_TEST_PLAN_MARKER}\n\nbody")
    assert carries_bot_plan_marker(f"{BATCH_POINTER_MARKER} SK-2630")
    assert not carries_bot_plan_marker("QA: this still fails on staging")


# ---------------------------------------------------------------------------
# The slots stay separate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_batch_post_leaves_the_single_ticket_plan_alone():
    """The whole point. A ticket's own plan must survive a batch plan being
    posted to the same ticket."""
    jira = JiraClient()
    single = _comment("single-1", TEST_PLAN_MARKER)
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text, kind=None):
        updated.append(comment_id)
        return {"id": comment_id}

    with patch.object(jira, "get_comments", return_value=[single]):
        with patch.object(jira, "update_comment", side_effect=fake_update):
            with patch.object(jira, "_create_comment", AsyncMock(return_value={"id": "new"})):
                result = await jira.post_comment(
                    "SK-2623", "batch plan", kind=PlanCommentKind.batch
                )

    assert updated == [], "the single-ticket plan comment was overwritten"
    assert result["updated"] is False
    assert result["id"] == "new"


@pytest.mark.asyncio
async def test_a_single_post_leaves_the_batch_plan_alone():
    """And the reverse: regenerating a ticket's own plan must not eat the batch
    plan comment sitting next to it."""
    jira = JiraClient()
    batch = _comment("batch-1", BATCH_TEST_PLAN_MARKER)
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text, kind=None):
        updated.append(comment_id)
        return {"id": comment_id}

    with patch.object(jira, "get_comments", return_value=[batch]):
        with patch.object(jira, "update_comment", side_effect=fake_update):
            with patch.object(jira, "_create_comment", AsyncMock(return_value={"id": "new"})):
                await jira.post_comment("SK-2623", "own plan", kind=PlanCommentKind.single)

    assert updated == [], "the batch plan comment was overwritten"


@pytest.mark.asyncio
async def test_reposting_a_batch_plan_reuses_the_batch_comment():
    """One batch slot per ticket, updated in place — the same rule regenerating
    a single-ticket plan already follows."""
    jira = JiraClient()
    comments = [_comment("single-1", TEST_PLAN_MARKER), _comment("batch-1", BATCH_TEST_PLAN_MARKER)]
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text, kind=None):
        updated.append(comment_id)
        return {"id": comment_id}

    with patch.object(jira, "get_comments", return_value=comments):
        with patch.object(jira, "update_comment", side_effect=fake_update):
            with patch.object(jira, "_create_comment", side_effect=AssertionError("should reuse")):
                result = await jira.post_comment(
                    "SK-2623", "batch plan v2", kind=PlanCommentKind.batch
                )

    assert updated == ["batch-1"]
    assert result["updated"] is True


# ---------------------------------------------------------------------------
# Comments left behind by the all-tickets-get-a-copy behaviour
# ---------------------------------------------------------------------------


def test_a_pre_split_batch_comment_is_recognised_by_its_footer():
    legacy = _comment("old", TEST_PLAN_MARKER, "Also posted to: SK-2624, SK-2625")
    assert _is_legacy_batch_comment(legacy)


def test_a_real_single_ticket_plan_is_not_mistaken_for_a_batch_one():
    """The footer is the only evidence. Without it the comment stays where it
    is — guessing would overwrite the plan this change exists to protect."""
    plain = _comment("own", TEST_PLAN_MARKER, "1. Open the share modal")
    assert not _is_legacy_batch_comment(plain)


@pytest.mark.asyncio
async def test_a_batch_post_adopts_the_comment_left_by_an_earlier_run():
    """Left alone, a pre-split batch comment sits in the single-ticket slot
    waiting for the next single-ticket post to overwrite it."""
    jira = JiraClient()
    legacy = _comment("old", TEST_PLAN_MARKER, "Also posted to: SK-2624, SK-2625")
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text, kind=None):
        updated.append(comment_id)
        return {"id": comment_id}

    with patch.object(jira, "get_comments", return_value=[legacy]):
        with patch.object(jira, "update_comment", side_effect=fake_update):
            with patch.object(jira, "_create_comment", side_effect=AssertionError("should adopt")):
                result = await jira.post_comment(
                    "SK-2623",
                    "batch plan",
                    kind=PlanCommentKind.batch,
                    adopt_legacy_batch=True,
                )

    assert updated == ["old"]
    assert result["adopted_legacy"] is True


@pytest.mark.asyncio
async def test_adoption_is_off_unless_the_caller_asks():
    jira = JiraClient()
    legacy = _comment("old", TEST_PLAN_MARKER, "Also posted to: SK-2624")

    with patch.object(jira, "get_comments", return_value=[legacy]):
        with patch.object(jira, "update_comment", side_effect=AssertionError("must not touch")):
            with patch.object(jira, "_create_comment", AsyncMock(return_value={"id": "new"})):
                result = await jira.post_comment(
                    "SK-2623", "batch plan", kind=PlanCommentKind.batch
                )

    assert result["adopted_legacy"] is False


@pytest.mark.asyncio
async def test_adoption_never_applies_to_a_single_ticket_post():
    """A single-ticket post reaching for a legacy batch comment would be the
    original bug wearing a different hat."""
    jira = JiraClient()
    legacy = _comment("old", TEST_PLAN_MARKER, "Also posted to: SK-2624")
    updated: list[str] = []

    async def fake_update(issue_key, comment_id, text, kind=None):
        updated.append(comment_id)
        return {"id": comment_id}

    with patch.object(jira, "get_comments", return_value=[legacy]):
        with patch.object(jira, "update_comment", side_effect=fake_update):
            result = await jira.post_comment(
                "SK-2623",
                "own plan",
                kind=PlanCommentKind.single,
                adopt_legacy_batch=True,
            )

    # It matched as a plain single-ticket comment, which is the pre-existing
    # behaviour — adoption did not widen what a single post is allowed to take.
    assert updated == ["old"]
    assert result["adopted_legacy"] is False


# ---------------------------------------------------------------------------
# Which ticket hosts
# ---------------------------------------------------------------------------


def test_the_ticket_with_the_most_branches_hosts():
    host = choose_batch_host([
        BatchHostCandidate("SK-2623", branch_count=1),
        BatchHostCandidate("SK-2630", branch_count=9),
        BatchHostCandidate("SK-2627", branch_count=4),
    ])
    assert host == "SK-2630"


def test_a_parent_in_the_batch_hosts_even_with_fewer_branches():
    """Deliberately absolute: the parent is the ticket a reader treats as the
    home of the group, and it outlives the children."""
    host = choose_batch_host([
        BatchHostCandidate("SK-2623", parent_key="SK-2665", branch_count=1),
        BatchHostCandidate("SK-2665", branch_count=0),
        BatchHostCandidate("SK-2630", parent_key="SK-2665", branch_count=9),
    ])
    assert host == "SK-2665"


def test_ties_break_on_the_lowest_key_numerically():
    """String ordering would put SK-10 before SK-9 and make the tie-break look
    arbitrary to anyone checking why a ticket won."""
    host = choose_batch_host([
        BatchHostCandidate("SK-10", branch_count=2),
        BatchHostCandidate("SK-9", branch_count=2),
    ])
    assert host == "SK-9"


def test_a_parent_chain_resolves_to_the_top_of_the_group():
    host = choose_batch_host([
        BatchHostCandidate("SK-1", parent_key="SK-2"),
        BatchHostCandidate("SK-2", parent_key="SK-3"),
        BatchHostCandidate("SK-3"),
    ])
    assert host == "SK-3"


@pytest.mark.asyncio
async def test_the_recorded_host_wins_over_a_fresh_computation():
    """Branch counts grow as work proceeds. Recomputing the host on every post
    would eventually move the plan and strand the copy on the old host."""
    run = MagicMock()
    run.batch_host_key = "SK-2665"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        host, recorded = await plan_posting.resolve_batch_host(
            plan_id=7, selected_keys=["SK-2623", "SK-2630", "SK-2665"]
        )

    assert host == "SK-2665"
    assert recorded == "SK-2665"


@pytest.mark.asyncio
async def test_a_host_left_out_of_the_selection_is_replaced():
    run = MagicMock()
    run.batch_host_key = "SK-2665"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        host, recorded = await plan_posting.resolve_batch_host(
            plan_id=7, selected_keys=["SK-2630", "SK-2623"]
        )

    assert host == "SK-2623", "fell through to the lowest-key tie-break"
    assert recorded == "SK-2665", "the dropped host is reported so it can be retired"


# ---------------------------------------------------------------------------
# The fan-out
# ---------------------------------------------------------------------------


def _fake_jira(posted):
    jira = MagicMock()

    async def post_comment(issue_key, comment_text, version_note=None,
                           kind=PlanCommentKind.single, adopt_legacy_batch=False):
        posted.append((issue_key, kind, comment_text))
        return {
            "id": f"c-{issue_key}", "updated": False, "truncated": False,
            "parts": 1, "posted_parts": 1, "part_comment_ids": [f"c-{issue_key}"],
            "stale_parts_left": 0, "part_error": None, "kind": kind.value,
            "adopted_legacy": False,
        }

    jira.post_comment = post_comment
    jira.get_comments = AsyncMock(return_value=[])
    return jira


@pytest.mark.asyncio
async def test_the_plan_goes_to_the_host_and_pointers_to_the_rest():
    posted: list = []
    jira = _fake_jira(posted)
    run = MagicMock()
    run.batch_host_key = "SK-2630"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)
    repo.get_plan_with_cases = AsyncMock(return_value=None)
    repo.mark_plan_posted_to_jira = AsyncMock()

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira,
            # The recorded host is only consulted for a plan that was persisted;
            # without an id there is no run to read it off.
            plan_id=7,
            comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630", "SK-2627"],
        )

    assert result["host_key"] == "SK-2630"
    kinds = {key: kind for key, kind, _ in posted}
    assert kinds["SK-2630"] is PlanCommentKind.batch
    assert kinds["SK-2623"] is PlanCommentKind.pointer
    assert kinds["SK-2627"] is PlanCommentKind.pointer
    assert len(result["pointers"]) == 2

    # The plan text goes to exactly one ticket.
    plan_targets = [key for key, kind, text in posted if text == "the plan"]
    assert plan_targets == ["SK-2630"]


@pytest.mark.asyncio
async def test_the_pointer_names_the_host():
    posted: list = []
    jira = _fake_jira(posted)
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        await plan_posting.post_batch(
            jira, plan_id=None, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    pointer_text = next(t for k, kind, t in posted if kind is PlanCommentKind.pointer)
    assert "SK-2623" in pointer_text, "the pointer has to name where the plan is"


@pytest.mark.asyncio
async def test_no_pointers_are_left_when_the_plan_never_landed():
    """A pointer written before the plan exists sends the tester to a ticket
    with nothing on it."""
    posted: list = []
    jira = _fake_jira(posted)

    async def failed_host(issue_key, comment_text, version_note=None,
                          kind=PlanCommentKind.single, adopt_legacy_batch=False):
        posted.append((issue_key, kind, comment_text))
        return {
            "id": None, "updated": False, "truncated": False, "parts": 3,
            "posted_parts": 0, "part_comment_ids": [], "stale_parts_left": 0,
            "part_error": "Failed to reach Jira", "kind": kind.value,
            "adopted_legacy": False,
        }

    jira.post_comment = failed_host
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=None, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    assert result["pointers"] == []
    assert [k for k, _, _ in posted] == ["SK-2623"]


@pytest.mark.asyncio
async def test_a_failed_pointer_does_not_fail_the_post():
    """The plan is on the host, which is what matters. Raising here would
    describe a successful post as a failed one."""
    posted: list = []
    calls = {"n": 0}

    async def flaky(issue_key, comment_text, version_note=None,
                    kind=PlanCommentKind.single, adopt_legacy_batch=False):
        calls["n"] += 1
        if kind is PlanCommentKind.pointer and calls["n"] == 2:
            raise RuntimeError("Jira said no")
        posted.append((issue_key, kind, comment_text))
        return {
            "id": f"c-{issue_key}", "updated": False, "truncated": False,
            "parts": 1, "posted_parts": 1, "part_comment_ids": [f"c-{issue_key}"],
            "stale_parts_left": 0, "part_error": None, "kind": kind.value,
            "adopted_legacy": False,
        }

    jira = MagicMock()
    jira.post_comment = flaky
    jira.get_comments = AsyncMock(return_value=[])
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=None, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630", "SK-2627"],
        )

    assert result["success"] is True
    assert len(result["pointer_errors"]) == 1
    assert len(result["pointers"]) == 1


@pytest.mark.asyncio
async def test_a_dropped_host_has_its_plan_replaced_with_a_pointer():
    """Otherwise it keeps a full batch plan nobody updates any more — a second,
    silently stale copy."""
    posted: list = []
    deleted: list = []
    jira = _fake_jira(posted)
    # Only the dropped host carries the old plan; the tickets still in the
    # batch have none, which is what makes this the out-of-selection case
    # rather than the demotion one.
    jira.get_comments = AsyncMock(side_effect=lambda key: (
        [_comment("batch-old", BATCH_TEST_PLAN_MARKER)] if key == "SK-2665" else []
    ))
    jira.delete_comment = AsyncMock(side_effect=lambda key, cid: deleted.append((key, cid)))

    run = MagicMock()
    run.batch_host_key = "SK-2665"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=7, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    assert result["moved_from"] == "SK-2665"
    assert deleted == [("SK-2665", "batch-old")], "the stale plan must go before the pointer"
    assert ("SK-2665", PlanCommentKind.pointer) in [(k, kind) for k, kind, _ in posted]


@pytest.mark.asyncio
async def test_an_unticked_ticket_with_no_batch_comment_is_left_alone():
    """Narrow on purpose: retirement rewrites a comment the bot already wrote
    and never creates one on a ticket the tester deselected."""
    posted: list = []
    jira = _fake_jira(posted)
    jira.get_comments = AsyncMock(return_value=[_comment("own", TEST_PLAN_MARKER)])
    jira.delete_comment = AsyncMock(side_effect=AssertionError("must not delete"))

    run = MagicMock()
    run.batch_host_key = "SK-2665"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=7, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    assert result["moved_from"] is None
    assert "SK-2665" not in [k for k, _, _ in posted]


@pytest.mark.asyncio
async def test_the_host_records_the_batch_plan_in_its_own_slot():
    """`mark_plan_posted_to_jira` is scoped, so marking the batch plan live
    must not clear the ticket's own plan — it would read "Not live in Jira"
    with its comment sitting right there."""
    jira = _fake_jira([])
    repo = MagicMock()
    repo.get_plan_with_cases = AsyncMock(return_value=None)
    repo.mark_plan_posted_to_jira = AsyncMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        await plan_posting.post_batch(
            jira, plan_id=7, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    repo.mark_plan_posted_to_jira.assert_awaited_once()
    assert repo.mark_plan_posted_to_jira.await_args.kwargs["batch"] is True


@pytest.mark.asyncio
async def test_a_pointer_is_never_recorded_as_a_live_plan_version():
    """A signpost is not a version of the plan."""
    jira = _fake_jira([])
    repo = MagicMock()
    repo.get_plan_with_cases = AsyncMock(return_value=None)
    repo.mark_plan_posted_to_jira = AsyncMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=7, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630", "SK-2627"],
        )

    assert all(p["recorded"] is None for p in result["pointers"])
    assert repo.mark_plan_posted_to_jira.await_count == 1


@pytest.mark.asyncio
async def test_a_pointer_is_not_a_plan_comment():
    """`plan_adoption` reads plans back out of Jira comments through this
    predicate. A pointer holds no test cases, so adopting one would persist a
    two-line signpost as a ticket's test plan."""
    from src.app.jira_client import _comment_carries_test_plan_marker

    assert _comment_carries_test_plan_marker(_comment("1", TEST_PLAN_MARKER))
    assert _comment_carries_test_plan_marker(_comment("2", BATCH_TEST_PLAN_MARKER))
    assert not _comment_carries_test_plan_marker(
        _comment("3", f"{BATCH_POINTER_MARKER} SK-2630")
    )
    assert not _comment_carries_test_plan_marker(_comment("4", "a human comment"))


def test_pull_requests_count_towards_hosting_when_branches_do_not():
    """Branches were the rule as stated, but this Jira reports zero branches on
    every ticket while PR counts run 1..18 — `_extract_branches` reads the
    `repository` dev-status detail, which is not populated here. Ranking on
    branches alone would silently degrade to the lowest-key tie-break."""
    host = choose_batch_host([
        BatchHostCandidate("SK-2623", branch_count=0, pr_count=5),
        BatchHostCandidate("SK-2630", branch_count=0, pr_count=18),
        BatchHostCandidate("SK-2627", branch_count=0, pr_count=1),
    ])
    assert host == "SK-2630"


def test_a_parent_in_the_batch_still_outranks_every_pr():
    host = choose_batch_host([
        BatchHostCandidate("SK-2620", pr_count=0),
        BatchHostCandidate("SK-2630", parent_key="SK-2620", pr_count=18),
    ])
    assert host == "SK-2620"


def test_candidates_read_both_counts_off_a_ticket():
    from src.app.services.batch_plan_host import candidate_from_ticket

    ticket = MagicMock()
    ticket.ticket_key = "SK-2630"
    ticket.parent_info = {"key": "sk-2620"}
    ticket.development_info = {"branches": [], "pull_requests": [{}, {}, {}]}

    c = candidate_from_ticket(ticket)
    assert c.parent_key == "SK-2620", "parent keys are normalised for comparison"
    assert c.linked_work == 3


@pytest.mark.asyncio
async def test_a_host_demoted_while_still_selected_loses_its_plan():
    """The recorded host can change while every ticket stays in the batch —
    backfilling `batch_host_key` does exactly that. `_retire_previous_host`
    does not cover it (it only looks at tickets dropped from the batch), so
    without this the old host carries a full plan *and* a pointer saying the
    plan is elsewhere."""
    posted: list = []
    deleted: list = []
    jira = _fake_jira(posted)
    jira.get_comments = AsyncMock(side_effect=lambda key: (
        [_comment("batch-old", BATCH_TEST_PLAN_MARKER)] if key == "SK-2623" else []
    ))
    jira.delete_comment = AsyncMock(side_effect=lambda key, cid: deleted.append((key, cid)))

    run = MagicMock()
    run.batch_host_key = "SK-2630"
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)
    repo.get_plan_with_cases = AsyncMock(return_value=None)
    repo.mark_plan_posted_to_jira = AsyncMock()

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=7, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630", "SK-2627"],
        )

    assert result["host_key"] == "SK-2630"
    assert result["demoted"] == ["SK-2623"]
    assert deleted == [("SK-2623", "batch-old")]
    # And it does carry the pointer, so the old host is not a dead end.
    assert ("SK-2623", PlanCommentKind.pointer) in [(k, kind) for k, kind, _ in posted]


@pytest.mark.asyncio
async def test_a_ticket_that_never_hosted_is_not_touched_by_the_sweep():
    posted: list = []
    jira = _fake_jira(posted)
    jira.get_comments = AsyncMock(return_value=[_comment("own", TEST_PLAN_MARKER)])
    jira.delete_comment = AsyncMock(side_effect=AssertionError("must not delete"))
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=None)

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        result = await plan_posting.post_batch(
            jira, plan_id=None, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2630"],
        )

    assert result["demoted"] == []


# ---------------------------------------------------------------------------
# The pointer says what the batch plan checked about this ticket (SK-2627)
# ---------------------------------------------------------------------------


def test_a_pointer_for_a_ticket_with_no_extracted_acs_says_nothing_was_checked():
    text = plan_posting._pointer_text(
        "SK-2630", ["SK-2627", "SK-2630"], {"total": 0, "uncovered": [], "under_covered": []}
    )
    assert "No acceptance criteria could be read from this ticket" in text


def test_a_pointer_names_destination_gaps():
    text = plan_posting._pointer_text(
        "SK-2630",
        ["SK-2627", "SK-2630"],
        {
            "total": 15,
            "uncovered": [{"id": "SK-2627-AC4", "text": "Body"}],
            "under_covered": [
                {
                    "id": "SK-2627-AC7",
                    "missing_actions": ["destination: directly inside the Forms file"],
                    "missing_destination": "directly inside the Forms file",
                }
            ],
        },
    )
    assert "covers 14 of this ticket's 15 acceptance criteria" in text
    assert "SK-2627-AC4 (not covered)" in text
    assert "SK-2627-AC7 (no case asserts where it lands: directly inside the Forms file)" in text


def test_an_unreadable_plan_is_not_reported_as_one_without_acs():
    text = plan_posting._pointer_text(
        "SK-2630", ["SK-2627"], plan_posting.COVERAGE_UNREADABLE
    )
    assert "could not be loaded" in text
    assert "No acceptance criteria" not in text


@pytest.mark.asyncio
async def test_each_pointer_carries_its_own_tickets_coverage():
    import json as _json

    posted: list = []
    jira = _fake_jira(posted)
    run = MagicMock()
    run.batch_host_key = "SK-2630"
    plan = MagicMock()
    plan.body = _json.dumps({"ac_coverage": {"tickets": {
        "SK-2627": {"total": 0, "uncovered": [], "under_covered": []},
        "SK-2623": {"total": 2, "uncovered": [], "under_covered": []},
    }}})
    repo = MagicMock()
    repo.get_run_for_plan = AsyncMock(return_value=run)
    repo.get_plan_with_cases = AsyncMock(return_value=(plan, []))
    repo.mark_plan_posted_to_jira = AsyncMock()

    with (
        patch("src.app.services.plan_posting.plan_repository", repo),
        patch("src.app.services.plan_posting.get_db", return_value=MagicMock()),
    ):
        await plan_posting.post_batch(
            jira, plan_id=551, comment_text="the plan",
            ticket_keys=["SK-2623", "SK-2627", "SK-2630"],
        )

    bodies = {key: text for key, kind, text in posted if kind is PlanCommentKind.pointer}
    assert "No acceptance criteria could be read" in bodies["SK-2627"]
    assert "covers 2 of this ticket's 2" in bodies["SK-2623"]
