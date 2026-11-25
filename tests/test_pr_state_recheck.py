"""Re-checking a plan's grounding PRs before save and before post.

The regression is SK-3084 plan 728 v1 (sanitized in tests/fixtures/sk3084):
written while PR #31 was open, posted the next day after #31 had merged with
the same head. The posted plan still blocked four cases on "PR #31 is not
merged", read "#31 — open — not yet merged @ c1d2c33", said the PR was a
draft not deployed anywhere, and its build check named only PR #30's merge.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.app import build_targets as bt
from src.app.pr_state_recheck import (
    PHASE_POST,
    PHASE_SAVE,
    recheck_pr_states,
    sha_suffix,
)
from src.app.services import auto_post, post_recheck

from .conftest import noop_get_db

pytestmark = pytest.mark.asyncio

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sk3084" / "plan728_v1.json").read_text())
REPO = "example-org/mobile-functions"
URL30 = f"https://github.com/{REPO}/pull/30"
URL31 = f"https://github.com/{REPO}/pull/31"
H30, M30 = "a30a30a" + "0" * 33, "577c933" + "0" * 33
H31, M31 = "c1d2c33" + "0" * 33, "7e106a0" + "0" * 33

CITED = "functions/src/majors/handler.ts"


def _plan():
    return copy.deepcopy(FIXTURE)


class FakeGitHub:
    """GitHub as it stood at post time: #31 merged, head unchanged.

    The merged tree differs from the grounded head only in CODEOWNERS, a file
    the PR also lists but no case cites.
    """

    def __init__(self, *, head31=H31, state31="merged", cited_at_merge=None, unreadable=(), unread_files=()):
        self.head31, self.state31 = head31, state31
        self.cited_at_merge = cited_at_merge
        self.unreadable, self.unread_files = set(unreadable), set(unread_files)

    async def fetch_pr_details(self, url, include_patch=False, include_comments=True):
        if url in self.unreadable:
            return None
        files = lambda *names: [SimpleNamespace(filename=n, status="modified") for n in names]  # noqa: E731
        if url == URL30:
            return SimpleNamespace(state="closed", merged=True, head_sha=H30, merge_commit_sha=M30,
                                   merged_at="2026-11-17T14:02:00Z",
                                   files_changed=files("functions/src/majors/validate.ts"))
        merged = self.state31 == "merged"
        return SimpleNamespace(
            state="open" if self.state31 == "open" else "closed", merged=merged, head_sha=self.head31,
            merge_commit_sha=M31 if merged else "fffffff" + "0" * 33,
            merged_at="2026-11-17T16:38:09Z" if merged else None,
            files_changed=files(CITED, "CODEOWNERS"))

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        out = {}
        for p in paths:
            if p in self.unread_files:
                continue
            if p == CITED:
                out[p] = (self.cited_at_merge if (ref == M31 and self.cited_at_merge) else "export const handler = 1\n")
            elif p == "CODEOWNERS":
                out[p] = "* @team-a\n" if ref == H31 else "* @team-b\n"
            else:
                out[p] = "same\n"
        return out


def _all_blocker_text(plan):
    out = []
    for section in ("happy_path", "edge_cases", "integration_tests"):
        for c in plan.get(section) or []:
            out += [c.get("preconditions") or "", c.get("blocked_by_setup") or ""]
    return " ".join(out)


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

async def test_sk3084_merged_after_generation_is_refreshed_at_post_time():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(),
                                     created_at="2026-11-17T15:59:41Z")

    assert report["refuse"] == []
    assert report["modified"] is True

    # No case is blocked on "PR #31 is not merged" any more.
    assert "not merged" not in _all_blocker_text(plan)
    for c in plan["edge_cases"] + [plan["integration_tests"][2]]:
        assert "blocked_by_setup" not in c
        assert "BLOCKED BY SETUP" not in c["preconditions"]
        assert "PR state re-checked at post time" in c["preconditions"]
        assert "7e106a0" in c["preconditions"]
        assert c["grounded_in_unmerged"] is False
    # The unrelated blocker on integration_tests:1 stays, alone.
    int1 = plan["integration_tests"][1]
    assert int1["blocked_by_setup"] == "ingestion deploy needs Lead approval"
    assert "BLOCKED BY SETUP (rule AF): ingestion deploy needs Lead approval — run once it exists." in int1["preconditions"]

    # GROUNDED IN: merged, with the merge SHA.
    e31 = next(e for e in plan["source_provenance"]["pull_requests"] if e["number"] == 31)
    assert e31["state"] == "merged"
    assert e31["merge_commit_sha"] == M31
    assert e31["state_at_generation"] == "open"
    assert sha_suffix(e31).startswith(" @ merge 7e106a0 (head c1d2c33)")
    assert plan["source_provenance"]["grounded_on_unmerged"] is False

    # The build check lists both merge commits.
    step = plan["build_check"]["step"]
    assert "577c933" in step and "7e106a0" in step
    assert step.rstrip().endswith("the case is blocked on deployment.")

    # DEPLOYMENT no longer says #31 is a draft.
    warnings = plan["deployment_status"]["warnings"]
    assert not any(w.startswith("The majors PR (#31) is still a draft") for w in warnings)
    assert any("merged" in w and "7e106a0" in w for w in warnings)
    assert plan["deployment_status"]["status"] == "unknown"

    # A visible note names what changed.
    note = plan["pr_state_refresh"]
    assert [c["pr"] for c in note["changes"]] == [f"{REPO}#31"]
    assert note["changes"][0]["files_compared"] == [CITED]
    assert len(note["blockers_dropped"]) == 4

    # Progress key: no case added, removed or moved.
    for section in ("happy_path", "edge_cases", "integration_tests", "regression_checklist"):
        assert len(plan[section]) == len(FIXTURE[section])


async def test_sk3084_posted_comment_reads_merged(tmp_path):
    """Through the real Jira formatter (node), as the Post button renders it."""
    plan = _plan()
    await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(), created_at="2026-11-17T15:59:41Z")
    text = await auto_post.render_jira_text(plan, None)

    assert "PR STATE REFRESHED AT POST TIME" in text
    assert f"{REPO}#31 — merged @ merge 7e106a0 (head c1d2c33)" in text
    assert "open — not yet merged" not in text
    # "PR #31 is not merged" appears only where the refresh note says it was removed.
    assert all(line.lstrip().startswith("• Blocker removed from")
               for line in text.splitlines() if "is not merged" in line)
    assert "BLOCKED BY SETUP (rule AF): PR #31" not in text
    assert "BLOCKED BY SETUP (rule AF): ingestion deploy needs Lead approval" in text
    # The draft sentence survives only quoted, inside the note that retracts it.
    assert all("no longer holds" in line for line in text.splitlines() if "still a draft" in line)
    build_line = next(line for line in text.splitlines() if "Build check —" in line)
    assert "577c933" in build_line and "7e106a0" in build_line


async def test_owner_fragment_goes_with_its_blocker_and_stale_prose_is_marked():
    # From the live plan 728: the blocker carried its owner as a second clause,
    # and the model repeated the claim as prose ("draft PR #31").
    plan = _plan()
    case = plan["edge_cases"][0]
    case["blocked_by_setup"] = "PR #31 is not merged or deployed; owner Jordan Example"
    case["preconditions"] = ("BLOCKED BY SETUP (rule AF): PR #31 is not merged or deployed; owner Jordan Example "
                             "— run once it exists. Blocked until draft PR #31 is deployed to integ. Precondition: S2 passed.")
    plan["shared_preconditions"] = [{"id": "S5", "text": "PR #31 is still a draft. Use the integ project."}]
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub())

    assert "blocked_by_setup" not in case
    assert "BLOCKED BY SETUP" not in case["preconditions"]
    assert "owner Jordan Example" not in case["preconditions"]
    assert "Blocked until draft PR #31 is deployed to integ. [stale: example-org/mobile-functions#31 has since merged as 7e106a0" in case["preconditions"]
    assert plan["shared_preconditions"][0]["text"].startswith("PR #31 is still a draft. [stale:")
    assert plan["shared_preconditions"][0]["text"].endswith("Use the integ project.")
    assert {"case": "S5", "field": "text"} in report["stale_sentences_marked"]


# ---------------------------------------------------------------------------
# Refusals: grounding out of date
# ---------------------------------------------------------------------------

async def test_new_commits_on_the_pr_refuse_and_leave_the_plan_untouched():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(head31="d00d00d" + "0" * 33))
    assert report["refuse"] and "new commits" in report["refuse"][0]
    assert plan == FIXTURE


async def test_cited_file_changed_in_the_merge_refuses():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST,
                                     github=FakeGitHub(cited_at_merge="export const handler = 2\n"))
    assert report["refuse"] and CITED in report["refuse"][0]
    assert plan == FIXTURE


async def test_merged_tree_that_cannot_be_read_refuses():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(unread_files={CITED}))
    assert report["refuse"] and "could not be confirmed" in report["refuse"][0]
    assert plan == FIXTURE


async def test_closed_without_merging_refuses():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(state31="closed"))
    assert report["refuse"] and "closed without merging" in report["refuse"][0]


# ---------------------------------------------------------------------------
# Unchanged and unreadable
# ---------------------------------------------------------------------------

async def test_still_open_keeps_the_blockers():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(state31="open"))
    assert report["refuse"] == [] and report["changes"] == []
    assert plan["edge_cases"][0]["blocked_by_setup"] == "PR #31 is not merged or deployed"
    assert "pr_state_refresh" not in plan


async def test_unreadable_at_post_marks_blockers_unverified_not_asserted():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(unreadable={URL31}))
    assert report["refuse"] == []
    case = plan["edge_cases"][0]
    assert "unverified at post time" in case["blocked_by_setup"]
    assert "unverified at post time" in case["preconditions"]
    assert plan["pr_state_refresh"]["unreadable"] == [f"{REPO}#31"]
    int1 = plan["integration_tests"][1]["blocked_by_setup"]
    assert int1.endswith("; ingestion deploy needs Lead approval")


async def test_unreadable_at_save_leaves_the_fresh_reading_alone():
    plan = _plan()
    report = await recheck_pr_states(plan, phase=PHASE_SAVE, github=FakeGitHub(unreadable={URL30, URL31}))
    assert report["modified"] is False
    assert plan["edge_cases"] == FIXTURE["edge_cases"]


async def test_blocker_naming_a_pr_already_merged_at_generation_is_dropped_before_save():
    # Rule 6: the blocker's stated reason must match the PR's state, whatever changed.
    plan = _plan()
    plan["happy_path"][0]["blocked_by_setup"] = "PR #30 is not merged yet"
    report = await recheck_pr_states(plan, phase=PHASE_SAVE, github=FakeGitHub(state31="open"))
    assert "blocked_by_setup" not in plan["happy_path"][0]
    assert report["blockers_dropped"][0]["case"] == "happy_path:0"


async def test_a_plan_with_no_grounding_prs_is_not_touched():
    plan = {"happy_path": [{"title": "x", "blocked_by_setup": "PR #9 is not merged"}]}
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub())
    assert report["checked"] == 0 and report["modified"] is False


# ---------------------------------------------------------------------------
# Post paths
# ---------------------------------------------------------------------------

def _stored_fixture():
    from datetime import datetime, timezone

    return SimpleNamespace(id=728, version=1, body=json.dumps(FIXTURE),
                           created_at=datetime(2026, 11, 17, 15, 59, 41, tzinfo=timezone.utc))


async def test_manual_post_rerenders_and_saves_the_refreshed_plan():
    repo = post_recheck.plan_repository
    update = AsyncMock()
    with patch.object(post_recheck, "get_db", noop_get_db), \
            patch.object(repo, "get_plan_with_cases", AsyncMock(return_value=(_stored_fixture(), []))), \
            patch.object(repo, "update_plan_body", update), \
            patch("src.app.uat_readiness.fetch_readiness", AsyncMock(return_value=None)):
        text, report = await post_recheck.comment_text_for_post(
            728, "STALE TEXT FROM THE BROWSER", walkthrough_key="SK-3084", github=FakeGitHub())
    assert text != "STALE TEXT FROM THE BROWSER"
    assert "PR STATE REFRESHED AT POST TIME" in text
    saved = json.loads(update.await_args.kwargs["body"])
    assert saved["source_provenance"]["pull_requests"][1]["state"] == "merged"
    assert post_recheck.describe(report)["blockers_dropped"] == 4


async def test_manual_post_refuses_when_the_head_moved():
    repo = post_recheck.plan_repository
    with patch.object(post_recheck, "get_db", noop_get_db), \
            patch.object(repo, "get_plan_with_cases", AsyncMock(return_value=(_stored_fixture(), []))):
        with pytest.raises(post_recheck.PlanGroundingStale) as exc:
            await post_recheck.comment_text_for_post(
                728, "text", walkthrough_key="SK-3084", github=FakeGitHub(head31="d00d00d" + "0" * 33))
    assert "new commits" in str(exc.value)


async def test_manual_post_without_changes_posts_the_browser_text():
    repo = post_recheck.plan_repository
    with patch.object(post_recheck, "get_db", noop_get_db), \
            patch.object(repo, "get_plan_with_cases", AsyncMock(return_value=(_stored_fixture(), []))):
        text, report = await post_recheck.comment_text_for_post(
            728, "browser text", walkthrough_key="SK-3084", github=FakeGitHub(state31="open"))
    assert text == "browser text" and report["modified"] is False


async def test_freshness_ignores_a_merge_the_recheck_already_accounted_for():
    from datetime import datetime, timezone

    serialized = {"development_info": {"pull_requests": [
        {"url": URL31, "status": "MERGED", "merged_at": "2026-11-17T16:38:09Z"}]}}
    created = datetime(2026, 11, 17, 15, 59, 41, tzinfo=timezone.utc)
    assert auto_post.freshness_problem(serialized, created)[1] == auto_post.SKIP_STALE_PLAN
    assert auto_post.freshness_problem(serialized, created, accounted_for={URL31}) is None


# ---------------------------------------------------------------------------
# Build gate at generation: every merged PR is named
# ---------------------------------------------------------------------------

async def test_gate_sentence_names_every_later_merged_pr():
    def pr(n, sha, at):
        return {"repository": REPO, "number": n, "status": "MERGED", "merge_commit_sha": sha, "merged_at": at,
                "head_sha": sha}

    [t] = bt.build_targets([pr(30, M30, "2026-11-17T14"), pr(31, M31, "2026-11-17T16"),
                            pr(32, "9999999" + "0" * 33, "2026-11-17T18")])
    s = bt.gate_sentence(t)
    assert "PR #30 (merge commit 577c933)" in s
    assert "PR #31 (merge commit 7e106a0)" in s
    assert "Build must be at or after 9999999 (PR #32" in s


async def test_save_time_hook_refreshes_a_pr_that_merged_while_generating(monkeypatch):
    # Rule 5/6 before save: generation takes minutes, and a PR can merge in them.
    from src.app.config import settings
    from src.app.services import plan_service

    monkeypatch.setattr(settings, "pr_state_recheck_before_save", True)
    monkeypatch.setattr("src.app.github_client.GitHubClient", lambda *a, **k: FakeGitHub())
    plan = _plan()
    await plan_service._recheck_pr_states_before_save(plan)
    assert plan["pr_state_refresh"]["phase"] == "save"
    assert "blocked_by_setup" not in plan["edge_cases"][0]
    assert "PR state re-checked at save time" in plan["edge_cases"][0]["preconditions"]

    # Off for replays of historical inputs: nothing is read or changed.
    monkeypatch.setattr(settings, "pr_state_recheck_before_save", False)
    plan = _plan()
    await plan_service._recheck_pr_states_before_save(plan)
    assert plan == FIXTURE


async def test_unverified_mark_from_an_earlier_post_goes_once_the_state_is_read():
    plan = _plan()
    await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(unreadable={URL31}))
    assert "unverified at post time" in plan["edge_cases"][0]["blocked_by_setup"]
    # Next post: GitHub answers, PR still open. The blocker holds, unmarked.
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub(state31="open"))
    assert plan["edge_cases"][0]["blocked_by_setup"] == "PR #31 is not merged or deployed"
    assert "unverified" not in plan["edge_cases"][0]["preconditions"]
    assert plan["pr_state_refresh"]["unreadable"] == [] and plan["pr_state_refresh"]["blockers_unverified"] == []
    assert report["modified"] is True


async def test_a_refreshed_plan_keeps_its_note_and_does_not_change_on_the_next_post():
    plan = _plan()
    await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub())
    snapshot = copy.deepcopy(plan)
    report = await recheck_pr_states(plan, phase=PHASE_POST, github=FakeGitHub())
    assert report["modified"] is False and report["refuse"] == []
    assert plan["pr_state_refresh"] == snapshot["pr_state_refresh"]
    assert plan["edge_cases"] == snapshot["edge_cases"]
    assert plan["build_check"] == snapshot["build_check"]


async def test_auto_post_refusal_alert_does_not_offer_posting_as_is():
    out = auto_post.AutoPostOutcome("SK-3084", auto_post.SKIPPED, auto_post.SKIP_GROUNDING_MOVED,
                                    "x#31 has new commits since the plan was written", 728)
    assert "as it is" not in auto_post._alert_text(out)


async def test_auto_post_posts_the_refreshed_plan_instead_of_skipping_it_as_stale(monkeypatch):
    from unittest.mock import MagicMock

    repo = auto_post.plan_repository
    post_one = AsyncMock(return_value={"recorded": True, "comment_id": "c-1"})
    update = AsyncMock()
    jira = MagicMock()
    jira.get_comments = AsyncMock(return_value=[])
    jira.get_issue = AsyncMock(return_value=object())
    serialized = {"development_info": {"pull_requests": [
        {"url": URL30, "status": "MERGED", "merged_at": "2026-11-17T14:02:00Z"},
        {"url": URL31, "status": "MERGED", "merged_at": "2026-11-17T16:38:09Z"}]}}
    monkeypatch.setattr("src.app.github_client.GitHubClient", lambda *a, **k: FakeGitHub())
    with patch.object(auto_post, "get_db", noop_get_db), \
            patch.object(repo, "list_runs_with_plans_by_ticket",
                         AsyncMock(return_value=[{"plan_id": 728, "ticket_keys": ["SK-3084"], "case_count": 6}])), \
            patch.object(repo, "list_live_plans_for_ticket", AsyncMock(return_value=[])), \
            patch.object(repo, "get_plan_with_cases", AsyncMock(return_value=(_stored_fixture(), []))), \
            patch.object(repo, "update_plan_body", update), \
            patch.object(auto_post.uat_readiness, "fetch_readiness", AsyncMock(return_value={})), \
            patch.object(auto_post.plan_posting, "post_one", post_one), \
            patch.object(auto_post.auto_post_repository, "record", AsyncMock(return_value=None)), \
            patch.object(auto_post.auto_post_repository, "add_event", AsyncMock()), \
            patch.object(auto_post.auto_post_repository, "claim", AsyncMock(return_value=True)), \
            patch.object(auto_post.auto_post_repository, "release", AsyncMock()), \
            patch.object(auto_post, "_alert", lambda _o: None), \
            patch.object(auto_post.plan_service, "serialize_issue", lambda _i: serialized):
        out = await auto_post.auto_post_own_plan(jira, "SK-3084")

    assert out.action == auto_post.POSTED, out
    text = post_one.await_args.kwargs["comment_text"]
    assert "PR STATE REFRESHED AT POST TIME" in text
    assert "BLOCKED BY SETUP (rule AF): PR #31" not in text
    assert json.loads(update.await_args.kwargs["body"])["pr_state_refresh"]["changes"][0]["pr"].endswith("#31")
