"""The UAT review: the one human checkpoint, and the only path to the tracker.

A run that finished is a proposal. These tests hold what approval promises:

* nothing reaches the tracker before a person approves;
* an agent's pass starts to count on the checklist only on approval;
* a reviewer's override is recorded with the reviewer as actor;
* results for a plan that is no longer live are never posted;
* a post that stops partway says which step failed, and approving again
  resumes it without repeating what already landed;
* the comment is the browser's formatter's output, one slot per ticket.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.app.repositories import test_plan_progress_repository, uat_run_repository
from src.app.services import uat_review, uat_runs
from src.app.services.uat_runs import RunRefused
from tests.test_uat_runs import _live_plan, _pass, _start, api, db  # noqa: F401


class TrackerJira:
    """Records every tracker write; any step can be told to fail once."""

    def __init__(self, fail: set[str] | None = None):
        self.fail = set(fail or ())
        self.uploads: list[str] = []
        self.comments: list[tuple[str | None, str]] = []

    async def upload_attachments(self, key, files):
        if "attachments" in self.fail:
            self.fail.discard("attachments")
            raise ConnectionError("upload dropped")
        self.uploads += [name for name, _b, _m in files]
        return [{"id": f"a{len(self.uploads)}-{i}"} for i, _ in enumerate(files)]

    async def upsert_uat_results_comment(self, key, text, comment_id=None):
        if "comment" in self.fail:
            self.fail.discard("comment")
            raise ConnectionError("comment dropped")
        self.comments.append((comment_id, text))
        return {"id": comment_id or "c-9", "updated": comment_id is not None}


def _transition(moves: list, fail_once: bool = False):
    state = {"fail": fail_once}

    async def transition(jira, key, action):
        if state["fail"]:
            state["fail"] = False
            raise ConnectionError("transition dropped")
        moves.append((key, action))
        return {"action": action, "target_status": "X", "assigned_to": "Dev"}

    return transition


async def _finished_run(db, monkeypatch, *, agent_pass=True):
    await _live_plan(db)
    run = await _start(db, monkeypatch)
    if agent_pass:
        await uat_runs.record_result(db, run, leg="mobile", payload=_pass("happy_path:0"))
    for leg in run.legs:
        run = await uat_runs.finalize_leg(db, await uat_run_repository.get(db, run.id), leg, {})
    return await uat_run_repository.get(db, run.id)


async def _checked(db, run):
    view = await test_plan_progress_repository.get_progress(db, progress_key=run.progress_key)
    return set(json.loads(view.checked_ids)) if view else set()


@pytest.mark.asyncio
async def test_approve_counts_agent_passes_then_posts_and_moves(db, monkeypatch, tmp_path):
    run = await _finished_run(db, monkeypatch)
    assert run.status == "awaiting_review"
    Path(run.evidence_dir).mkdir(parents=True, exist_ok=True)
    (Path(run.evidence_dir) / "07-sk1-walkthrough-narrated.mp4").write_bytes(b"mp4")
    (Path(run.evidence_dir) / "03-sk1-save--FAIL-no-toast.png").write_bytes(b"png")
    assert "happy_path:0" not in await _checked(db, run)

    jira, moves = TrackerJira(), []
    run = await uat_review.approve(db, run, next_step="fail-to-in-progress", notes="toast missing",
                                   jira=jira, transition=_transition(moves))

    assert run.status == "approved"
    assert run.review["decision"] == "approved" and run.review["actor"]["kind"] == "human"
    assert run.tracker["outcome"] == "posted"
    assert jira.uploads == ["07-sk1-walkthrough-narrated.mp4", "03-sk1-save--FAIL-no-toast.png"]
    (_cid, text), = jira.comments
    assert text.startswith("🤖 UAT run results — plan v1")
    assert "Fail back to In Progress" in text and "toast missing" in text
    assert "07-sk1-walkthrough-narrated.mp4" in text
    assert moves == [("SK-1", "fail-to-in-progress")]
    assert "happy_path:0" in await _checked(db, run)


@pytest.mark.asyncio
async def test_nothing_posts_before_approval_and_reject_posts_nothing(db, monkeypatch):
    run = await _finished_run(db, monkeypatch)
    view = await uat_review.review_view(db, run)
    assert view["review"] is None and view["tracker"] == {}
    run = await uat_review.reject(db, run, notes="runner used the wrong account")
    assert run.status == "rejected" and run.tracker == {}
    assert "happy_path:0" not in await _checked(db, run)
    with pytest.raises(RunRefused) as e:
        await uat_review.approve(db, run, next_step=None, notes=None, jira=TrackerJira())
    assert e.value.code == "not_awaiting_review"


@pytest.mark.asyncio
async def test_stale_plan_is_never_posted(db, monkeypatch):
    run = await _finished_run(db, monkeypatch)
    await _live_plan(db, version=2)  # regenerated and posted after the run
    jira = TrackerJira()
    with pytest.raises(RunRefused) as e:
        await uat_review.approve(db, run, next_step=None, notes=None, jira=jira)
    assert e.value.code == "stale_plan" and "v2" in e.value.message
    run = await uat_run_repository.get(db, run.id)
    assert run.status == "awaiting_review" and jira.comments == [] and jira.uploads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["attachments", "comment", "transition"])
async def test_a_failed_step_is_named_and_approving_again_resumes(db, monkeypatch, step):
    run = await _finished_run(db, monkeypatch)
    Path(run.evidence_dir).mkdir(parents=True, exist_ok=True)
    (Path(run.evidence_dir) / "01-walkthrough.mp4").write_bytes(b"mp4")
    jira, moves = TrackerJira(fail={step} - {"transition"}), []
    transition = _transition(moves, fail_once=step == "transition")

    run = await uat_review.approve(db, run, next_step="pass-to-uat", notes=None,
                                   jira=jira, transition=transition)
    assert run.status == "approved"
    assert run.tracker["outcome"] == f"{step}_failed" and "dropped" in run.tracker["error"]

    run = await uat_review.approve(db, run, next_step="ignored-on-resume", notes=None,
                                   jira=jira, transition=transition)
    assert run.tracker["outcome"] == "posted"
    assert jira.uploads == ["01-walkthrough.mp4"]  # never uploaded twice
    assert moves == [("SK-1", "pass-to-uat")]  # moved once
    # A retried comment edits the one already written rather than adding one.
    assert [cid for cid, _ in jira.comments][1:] == ["c-9"] * (len(jira.comments) - 1)


@pytest.mark.asyncio
async def test_override_is_recorded_with_the_reviewer_and_closes_after_approval(db, monkeypatch):
    monkeypatch.setattr(uat_runs.settings, "qa_actor_email", "qa@example.com")
    run = await _finished_run(db, monkeypatch, agent_pass=False)
    with pytest.raises(RunRefused) as e:
        await uat_review.record_override(db, run, case_id="happy_path:0", status="failed")
    assert e.value.code == "note_required"
    await uat_review.record_override(db, run, case_id="happy_path:0", status="passed",
                                     note="checked on device")
    view = await uat_review.review_view(db, run)
    result = next(c["result"] for c in view["cases"] if c["id"] == "happy_path:0")
    assert result["status"] == "passed" and result["source"] == "review"
    assert result["actor"]["kind"] == "human" and result["actor"]["email"] == "qa@example.com"
    # A person's pass counts at once; it is not an agent proposal.
    assert "happy_path:0" in await _checked(db, run)

    run = await uat_review.approve(db, run, next_step=None, notes=None, jira=TrackerJira(),
                                   transition=_transition([]))
    with pytest.raises(RunRefused):
        await uat_review.record_override(db, run, case_id="happy_path:0", status="passed")


@pytest.mark.asyncio
async def test_no_next_step_posts_without_moving(db, monkeypatch):
    run = await _finished_run(db, monkeypatch)
    moves = []
    run = await uat_review.approve(db, run, next_step=None, notes=None, jira=TrackerJira(),
                                   transition=_transition(moves))
    assert run.tracker["outcome"] == "posted" and moves == []
    with pytest.raises(RunRefused) as e:
        other = await _finished_run_again(db, monkeypatch)
        await uat_review.approve(db, other, next_step="ship-it", notes=None, jira=TrackerJira())
    assert e.value.code == "bad_next_step"


async def _finished_run_again(db, monkeypatch):
    run = await _start(db, monkeypatch)
    for leg in run.legs:
        run = await uat_runs.finalize_leg(db, await uat_run_repository.get(db, run.id), leg, {})
    return await uat_run_repository.get(db, run.id)


def test_derived_next_step():
    assert uat_review.derive_next_step({"passed": 3, "failed": 1, "not_run": 2}) == "fail-to-in-progress"
    assert uat_review.derive_next_step({"passed": 3, "failed": 0, "not_run": 2}) is None
    assert uat_review.derive_next_step({"passed": 3, "failed": 0, "not_run": 0}) == "pass-to-uat"
    assert uat_review.derive_next_step({}) is None


@pytest.mark.asyncio
async def test_routes_review_override_approve(db, monkeypatch, api):
    run = await _finished_run(db, monkeypatch)
    monkeypatch.setattr(uat_review, "JiraClient", TrackerJira)

    async def no_move(jira, key, action):
        raise AssertionError("no next step was chosen")

    monkeypatch.setattr("src.app.workflow_routes.transition_and_assign", no_move)
    async with api as c:
        r = await c.get(f"/uat-runs/{run.id}/review")
        assert r.status_code == 200 and r.json()["derived_next_step"] is None
        r = await c.post(f"/uat-runs/{run.id}/review/results",
                         json={"case_id": "nope", "status": "passed"})
        assert r.status_code == 422 and r.json()["detail"]["error"] == "unknown_case"
        r = await c.post(f"/uat-runs/{run.id}/approve", json={"next_step": None})
        assert r.status_code == 200 and r.json()["tracker"]["outcome"] == "posted"
        r = await c.post(f"/uat-runs/{run.id}/approve", json={"next_step": None})
        assert r.status_code == 409 and r.json()["detail"]["error"] == "not_awaiting_review"
