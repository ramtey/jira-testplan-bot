"""An oversized plan is one index comment plus the full plan attached.

Why this file exists
--------------------
A plan too big for one Jira comment used to be split over up to five, and a
ticket carrying "part 1 of 4" … "part 4 of 4" is noise nobody reads in order
(SK-2687's batch plan: 19 cases, 90KB of ADF against a 30KB limit). Trimming
the prose could not fix it — dropping every metadata line still left 66KB — so
the full text now goes up as an attachment and the comment is an index of the
cases, linked to it.

The index is a new way for the comment to stop being the whole plan, so the
tests below pin what that must not break: every case id a tester marks against
is still in the comment, adoption reads the steps from the attachment instead
of adopting a hollow index, and no attachment is left behind or deleted by
mistake.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.app.jira_client import (
    JIRA_COMMENT_MAX_BYTES,
    PLAN_ATTACHMENT_PREFIX,
    JiraClient,
    JiraConnectionError,
    PlanCommentKind,
    TEST_PLAN_MARKER,
    _adf_size,
    _comment_body_adf,
    _plan_index_text,
    attached_plan_id,
    plan_attachment_filename,
)


def _case(section: str, i: int) -> str:
    return (
        f"**{i + 1}. [{section}:{i}] Verify the agent can submit listing {i} 🔴 CRITICAL**\n\n"
        "Preconditions: Agent is logged in with an active subscription and a draft listing.\n\n"
        "Steps:\n"
        "1. Navigate to the Listings tab from the main dashboard\n"
        "2. Tap the draft listing named \"123 Main St\"\n"
        "3. Fill in the required address, price, and square footage fields\n"
        "4. Tap Submit at the bottom of the form\n\n"
        f"Expected Result: Listing {i} is submitted and its status becomes Pending Review.\n\n"
        "Test Data: address=123 Main St, price=450000, sqft=2100\n\n"
        "Runs on: Web UI · credentials: agent account · environment: integ\n\n"
        "────────────────────────────────────────────\n\n"
    )


def _big_plan(per_section: int = 20) -> str:
    return (
        "⚠️ UI GROUNDING WARNINGS (2)\n\n"
        "Test steps reference these UI elements, but they could not be verified in the "
        "PR diff or testID reference. Confirm they exist before running the cases that name them.\n\n"
        "  • `SK-1-AC1` — Submit button: not in the diff\n"
        "  • `SK-1-AC2` — Pending badge: not in the diff\n\n"
        "✅ HAPPY PATH TEST CASES\n\n"
        + "".join(_case("happy_path", i) for i in range(per_section))
        + "🔍 EDGE CASES & ERROR SCENARIOS\n\n"
        + "".join(_case("edge_cases", i) for i in range(per_section))
        + "🔄 REGRESSION CHECKLIST\n\n"
        "  • [`regression_checklist:0`] Existing search still works\n\n"
        "⚠️ RISKS / GAPS OBSERVED\n\n"
        "Not test cases — gaps in the ticket itself that no case can mark pass or fail.\n\n"
        "  • Nobody says what happens offline\n"
        "    Impact: data loss\n\n"
        "🚧 NEEDS SPEC — NOT VERIFIABLE FROM SOURCE (1)\n\n"
        "1. Some unverifiable case\n"
        "   No source for it\n\n"
        "🧪 ALREADY COVERED BY UNIT TESTS (1)\n\n"
        "  • [`covered_by_unit_test:0`] Saves zero (from `happy_path`)\n"
    )


_LINE = f"{PLAN_ATTACHMENT_PREFIX} Full plan: [test-plan-SK-1.md](https://x.atlassian.net/secure/attachment/987/test-plan-SK-1.md)"


# --- The index text ------------------------------------------------------


def test_the_index_keeps_every_case_id_and_drops_the_steps():
    """The ids are what a tester marks against, so every one stays; the steps
    are what the attachment is for."""
    marked = f"{TEST_PLAN_MARKER}\n\n{_big_plan()}"
    index = _plan_index_text(marked, _LINE)

    for i in range(20):
        assert f"[happy_path:{i}]" in index
        assert f"[edge_cases:{i}]" in index
        assert f"Listing {i} is submitted" in index
    assert "Navigate to the Listings tab" not in index
    assert "Preconditions:" not in index
    assert "Test Data:" not in index
    assert "Runs on:" not in index


def test_the_index_keeps_the_markable_lists_and_collapses_the_rest():
    """Regression and covered items have ids a tester marks; warnings, risks and
    needs-spec cases are not gradeable and shrink to their heading."""
    index = _plan_index_text(f"{TEST_PLAN_MARKER}\n\n{_big_plan()}", _LINE)

    assert "regression_checklist:0" in index
    assert "covered_by_unit_test:0" in index
    assert "⚠️ UI GROUNDING WARNINGS (2) — in the attached plan" in index
    assert "Submit button: not in the diff" not in index
    assert "⚠️ RISKS / GAPS OBSERVED — in the attached plan" in index
    assert "Nobody says what happens offline" not in index
    assert "🚧 NEEDS SPEC — NOT VERIFIABLE FROM SOURCE (1) — in the attached plan" in index
    assert "Some unverifiable case" not in index


def test_the_index_fits_where_the_plan_did_not():
    marked = f"{TEST_PLAN_MARKER}\n\n{_big_plan()}"
    assert _adf_size(marked) > JIRA_COMMENT_MAX_BYTES, "fixture is not oversized"
    assert _adf_size(_plan_index_text(marked, _LINE)) <= JIRA_COMMENT_MAX_BYTES


def test_the_link_to_the_full_plan_is_visible_without_expanding_anything():
    adf = _comment_body_adf(_plan_index_text(f"{TEST_PLAN_MARKER}\n\n{_big_plan()}", _LINE))

    assert adf["content"][0]["content"][0]["text"] == TEST_PLAN_MARKER
    assert adf["content"][1]["type"] == "paragraph"
    assert adf["content"][1]["content"][0]["text"].startswith(PLAN_ATTACHMENT_PREFIX)
    assert adf["content"][2]["type"] == "expand"
    assert attached_plan_id(adf) == "987"


def test_an_attachment_url_inside_the_plan_body_is_not_an_index():
    """Only the visible paragraph beside the marker marks an index. A plan that
    quotes an attachment link in a step must still adopt from its own text."""
    body = (
        f"{TEST_PLAN_MARKER}\n\n✅ HAPPY PATH TEST CASES\n\n"
        f"**1. [happy_path:0] Case**\n\n{_LINE}\n\n"
    )
    assert attached_plan_id(_comment_body_adf(body)) is None


# --- Posting -------------------------------------------------------------


class _FakeJira:
    """Records what `post_comment` sends. Patches go on the instance, which
    the conftest attachment guard explicitly allows."""

    def __init__(self, existing_attachments=None, existing_comments=None):
        self.jira = JiraClient()
        self.created: list[str] = []
        self.uploaded: list[tuple[str, bytes, str]] = []
        self.deleted_attachments: list[str] = []
        self.existing_attachments = existing_attachments or []
        self.existing_comments = existing_comments or []

    async def create(self, issue_key, text, kind=None):
        self.created.append(text)
        return {"id": f"c{len(self.created)}"}

    async def upload(self, issue_key, files):
        self.uploaded.extend(files)
        return [{"id": "555", "filename": files[0][0]}]

    async def attachments(self, issue_key):
        return self.existing_attachments

    async def delete_attachment(self, attachment_id):
        self.deleted_attachments.append(attachment_id)

    def patches(self, **overrides):
        j = self.jira
        targets = {
            "get_comments": self.existing_comments,
            "_create_comment": self.create,
            "upload_attachments": self.upload,
            "get_attachments": self.attachments,
            "delete_attachment": self.delete_attachment,
            **overrides,
        }
        stack = []
        for name, value in targets.items():
            if callable(value):
                stack.append(patch.object(j, name, side_effect=value))
            else:
                stack.append(patch.object(j, name, return_value=value))
        return stack


async def _post(fake: _FakeJira, text: str, **overrides) -> dict:
    from contextlib import ExitStack

    with ExitStack() as stack:
        for p in fake.patches(**overrides):
            stack.enter_context(p)
        return await fake.jira.post_comment(
            "SK-1", text, kind=overrides.pop("kind", PlanCommentKind.single)
        )


@pytest.mark.asyncio
async def test_an_oversized_plan_is_one_comment_with_the_plan_attached():
    fake = _FakeJira()
    result = await _post(fake, _big_plan())

    assert len(fake.created) == 1
    assert result["parts"] == 1
    assert result["attachment_filename"] == "test-plan-SK-1.md"
    assert result["attachment_error"] is None
    name, content, _mime = fake.uploaded[0]
    assert name == "test-plan-SK-1.md"
    # The attachment is the whole plan, marker included, so it parses exactly
    # as the comment would have.
    assert content.decode() == f"{TEST_PLAN_MARKER}\n\n{_big_plan()}"
    assert "/secure/attachment/555/test-plan-SK-1.md" in fake.created[0]
    assert "Navigate to the Listings tab" not in fake.created[0]


@pytest.mark.asyncio
async def test_a_failed_attach_falls_back_to_splitting_and_says_why():
    """An index pointing at a file that never arrived would hide the steps
    entirely. The old split is worse to read but complete."""
    fake = _FakeJira()

    async def refuse(issue_key, files):
        raise JiraConnectionError("Failed to reach Jira: attachments disabled")

    result = await _post(fake, _big_plan(), upload_attachments=refuse)

    assert len(fake.created) > 1
    assert result["attachment_filename"] is None
    assert "attachments disabled" in result["attachment_error"]
    assert all(PLAN_ATTACHMENT_PREFIX not in part for part in fake.created)


@pytest.mark.asyncio
async def test_a_regenerated_plan_replaces_its_own_file_and_nothing_else():
    fake = _FakeJira(
        existing_attachments=[
            {"id": "100", "filename": "test-plan-SK-1.md"},
            {"id": "101", "filename": "batch-test-plan-SK-1.md"},
            {"id": "102", "filename": "screenshot.png"},
        ]
    )
    # The upload's own id comes back in the listing too and must survive.
    fake.existing_attachments.append({"id": "555", "filename": "test-plan-SK-1.md"})

    await _post(fake, _big_plan())

    assert fake.deleted_attachments == ["100"]


@pytest.mark.asyncio
async def test_a_plan_that_now_fits_removes_the_file_nothing_links_to():
    fake = _FakeJira(existing_attachments=[{"id": "100", "filename": "test-plan-SK-1.md"}])

    result = await _post(fake, "A short plan that fits.")

    assert fake.uploaded == []
    assert result["attachment_filename"] is None
    assert fake.deleted_attachments == ["100"]


@pytest.mark.asyncio
async def test_a_comment_that_never_lands_takes_its_attachment_with_it():
    fake = _FakeJira()

    async def down(issue_key, text, kind=None):
        raise JiraConnectionError("down")

    with pytest.raises(JiraConnectionError):
        await _post(fake, _big_plan(), _create_comment=down)
    assert fake.deleted_attachments == ["555"]


def test_batch_and_single_plans_keep_separate_files():
    """A batch plan sits beside the ticket's own plan, never on top of it — and
    the cleanup deletes by filename, so the names must differ."""
    assert plan_attachment_filename("SK-1", PlanCommentKind.batch) != plan_attachment_filename(
        "SK-1", PlanCommentKind.single
    )


# --- Adoption ------------------------------------------------------------


class _StubJira:
    comments: list[dict] = []
    attachment_text: str | None = None

    async def get_comments(self, key):
        return list(type(self).comments)

    async def download_attachment_text(self, attachment_id):
        if type(self).attachment_text is None:
            raise JiraConnectionError("Failed to reach Jira: timeout")
        return type(self).attachment_text


def _index_comment(full_text: str) -> dict:
    marked = f"{TEST_PLAN_MARKER}\n\n{full_text}"
    index = _plan_index_text(marked, _LINE)
    return {"id": "900", "created": "2026-10-07T10:00:00.000-0700", "body": _comment_body_adf(index)}


@pytest.mark.asyncio
async def test_adopting_an_index_reads_the_steps_from_the_attachment(monkeypatch):
    from src.app import jira_client as jira_client_module
    from src.app.services import plan_adoption

    _StubJira.comments = [_index_comment(_big_plan())]
    _StubJira.attachment_text = f"{TEST_PLAN_MARKER}\n\n{_big_plan()}"
    monkeypatch.setattr(jira_client_module, "JiraClient", _StubJira)

    result = await plan_adoption.preview("SK-1")
    # 20 numbered cases plus the covered one, filed back where it came from.
    assert result["section_counts"]["happy_path"] == 21
    assert result["section_counts"]["edge_cases"] == 20
    assert result["comment_ids"] == ["900"]

    # The counts match the index too; the steps are what only the attachment has.
    jira = _StubJira()
    selection = await plan_adoption._select(jira, "SK-1", None, allow_incomplete=False)
    await plan_adoption._read_attached_plans(jira, selection)
    plan = plan_adoption._parse_selection(selection, "SK-1")
    assert plan["happy_path"][0]["steps"][0] == "Navigate to the Listings tab from the main dashboard"
    assert plan["edge_cases"][19]["preconditions"].startswith("Agent is logged in")


@pytest.mark.asyncio
async def test_an_unreadable_attachment_refuses_adoption_instead_of_adopting_the_index(
    monkeypatch,
):
    """The index alone parses to the right counts and the right key, with no
    steps in any case — a hollow plan nothing downstream would notice."""
    from src.app import jira_client as jira_client_module
    from src.app.services import plan_adoption

    _StubJira.comments = [_index_comment(_big_plan())]
    _StubJira.attachment_text = None
    monkeypatch.setattr(jira_client_module, "JiraClient", _StubJira)

    with pytest.raises(plan_adoption.PlanAdoptionError, match="without their steps"):
        await plan_adoption.preview("SK-1")


# --- The real renderer, end to end ---------------------------------------

MARKDOWN_JS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "utils" / "markdown.js"


@pytest.mark.skipif(
    shutil.which("node") is None or not MARKDOWN_JS.exists(),
    reason="needs node and frontend/src/utils/markdown.js",
)
@pytest.mark.asyncio
async def test_a_real_oversized_plan_round_trips_through_the_index(tmp_path, monkeypatch):
    """Rendered by the real markdown.js, posted as an index, adopted back from
    index + attachment: the same progress key the plan started with, and every
    case id the plan has is printed in the one comment."""
    from src.app import jira_client as jira_client_module
    from src.app.services import plan_adoption
    from src.app.services.progress_key import build_progress_key, case_index

    def case(i, **extra):
        return {
            "title": f"Case {i} does the thing it should",
            "priority": "high",
            "preconditions": "Logged in as the owner of a sent envelope with two signers. " * 2,
            "steps": [f"Step {s} of case {i}, described at the length real plans use." * 2 for s in range(5)],
            "expected": f"Case {i} shows the confirmation and the status changes to Signed.",
            "test_data": "Envelope with the owner and two external recipients.",
            "surface": "web_ui",
            "credentials": "owner session",
            "environment": "integ",
            "expected_verified": False,
            **extra,
        }

    plan = {
        "happy_path": [case(i) for i in range(10)],
        "edge_cases": [case(i, category="boundary") for i in range(10, 22)],
        "integration_tests": [case(22), case(23, covered_by_unit_test=True)],
        "regression_checklist": ["Existing search still works", "Login unaffected"],
    }
    script = tmp_path / "render.mjs"
    script.write_text(
        f"import {{ formatTestPlanAsJira }} from {json.dumps(str(MARKDOWN_JS))}\n"
        "process.stdout.write(formatTestPlanAsJira(JSON.parse(process.argv[2])))\n"
    )
    text = subprocess.run(
        ["node", str(script), json.dumps(plan)], capture_output=True, text=True, check=True
    ).stdout

    fake = _FakeJira()
    result = await _post(fake, text)
    assert result["parts"] == 1, "the real plan did not fit as an index"
    assert result["attachment_filename"] == "test-plan-SK-1.md"

    index_adf = _comment_body_adf(fake.created[0])
    for cid in case_index(json.dumps(plan)):
        assert cid in json.dumps(index_adf, ensure_ascii=False), f"{cid} missing from the index"

    _StubJira.comments = [{"id": "c1", "created": "2026-10-07T10:00:00.000-0700", "body": index_adf}]
    _StubJira.attachment_text = fake.uploaded[0][1].decode()
    monkeypatch.setattr(jira_client_module, "JiraClient", _StubJira)
    adopted = await plan_adoption.preview("SK-1")

    assert adopted["progress_key"] == build_progress_key(["SK-1"], json.dumps(plan))
