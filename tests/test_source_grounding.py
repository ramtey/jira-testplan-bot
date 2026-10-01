"""Tests for source grounding — which code a run is allowed to write cases
against, and what happens when there is none.

Anchored on the two plans generated on 2026-09-09 against
``skyslope/agent-calculator``:

* **SK-2563** (plan 458, run 799, 26 cases) — three ``integration_tests``
  cases described ``deriveUserCalculatorOverridesBackfillDecision`` and the
  ``backfill-user-calculator-overrides*`` tests. Those symbols existed only
  on the branch behind PR #1303, closed unmerged on 2026-09-08T19:56:25Z,
  ~25 hours before the plan was generated. The author's closing comment
  said the migration had been abandoned.
* **SK-2609** (plan 453, 10 cases) — no PR, no branch, no commit. All ten
  cases were speculation; four asserted a "Personal / Brokerage / Group
  defaults" control that does not exist in the product.

The four states each get a case here, because the whole design rests on
telling them apart:

    merged            grounds a plan, unchanged from before
    open              grounds a plan, labelled as not-yet-merged
    closed_unmerged   never grounds anything
    (no PR at all)    no plan; a report of what was searched
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.llm_client import LLMClient
from src.app.models import TestPlan
from src.app.services import plan_service
from src.app.services.test_plan_generator import quarantine_ungrounded_cases
from src.app.source_grounding import (
    PR_STATE_CLOSED_UNMERGED,
    PR_STATE_MERGED,
    PR_STATE_OPEN,
    PR_STATE_UNKNOWN,
    build_provenance,
    classify_pr_state,
    filter_development_info,
    has_grounding_source,
    merge_provenance,
    no_source_result,
    partition_pull_requests,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _merged_pr() -> dict:
    return {
        "title": "Reset userCalculatorFieldDefaults for internal testers",
        "status": "MERGED",
        "url": "https://github.com/skyslope/agent-calculator/pull/1310",
        "repository": "skyslope/agent-calculator",
        "number": 1310,
        "source_branch": "codex/SK-2563-reset-defaults",
        "head_sha": "1" * 40,
        "merged_at": "2026-09-09T14:02:00Z",
        "files_changed": [
            {
                "filename": "apps/server/src/defaults.ts",
                "status": "modified",
                "additions": 12,
                "deletions": 3,
                "changes": 15,
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
            }
        ],
    }


def _sk2563_closed_pr() -> dict:
    """PR #1303 — the real one, closed unmerged the day before the plan."""
    return {
        "title": "SK-2563 backfill user calculator overrides",
        "status": "DECLINED",
        "url": "https://github.com/skyslope/agent-calculator/pull/1303",
        "repository": "skyslope/agent-calculator",
        "number": 1303,
        "source_branch": "codex/SK-2563-defaults-migration",
        "head_sha": "2" * 40,
        "merged_at": None,
        "files_changed": [
            {
                "filename": "apps/server/scripts/backfill-user-calculator-overrides.ts",
                "status": "added",
                "additions": 240,
                "deletions": 0,
                "changes": 240,
                "patch": (
                    "@@ +1,20 @@\n"
                    "+export function deriveUserCalculatorOverridesBackfillDecision() {}\n"
                ),
            }
        ],
    }


def _open_pr() -> dict:
    return {
        "title": "SK-2700 group defaults popover",
        "status": "OPEN",
        "url": "https://github.com/skyslope/agent-calculator/pull/1401",
        "repository": "skyslope/agent-calculator",
        "number": 1401,
        "source_branch": "codex/SK-2700-group-defaults",
        "head_sha": "3" * 40,
        "merged_at": None,
        "files_changed": [
            {
                "filename": "apps/web/src/DefaultsPopover.tsx",
                "status": "modified",
                "additions": 40,
                "deletions": 2,
                "changes": 42,
                "patch": "@@ -1 +1 @@\n-a\n+b\n",
            }
        ],
    }


def _dev_info(*prs, branches=None, commits=None) -> dict:
    return {
        "pull_requests": list(prs),
        "branches": branches if branches is not None else [],
        "commits": commits if commits is not None else [],
    }


# ─── state classification ─────────────────────────────────────────────────────


def test_classify_recognizes_all_four_states():
    assert classify_pr_state(_merged_pr()) == PR_STATE_MERGED
    assert classify_pr_state(_open_pr()) == PR_STATE_OPEN
    assert classify_pr_state(_sk2563_closed_pr()) == PR_STATE_CLOSED_UNMERGED
    assert classify_pr_state({"title": "no status field"}) == PR_STATE_UNKNOWN


def test_github_closed_vocabulary_is_treated_as_closed_unmerged():
    """Jira's dev-status says DECLINED; GitHub says closed + merged=False.
    Both reach this dict as ``status``, and both mean abandoned."""
    assert classify_pr_state({"status": "closed"}) == PR_STATE_CLOSED_UNMERGED
    assert classify_pr_state({"status": "DECLINED"}) == PR_STATE_CLOSED_UNMERGED


def test_merged_at_beats_a_stale_jira_status():
    """Jira's mirror can lag GitHub by hours. A merge timestamp only ever
    comes from GitHub's own payload, so it wins."""
    pr = {"status": "OPEN", "merged_at": "2026-09-09T14:02:00Z"}
    assert classify_pr_state(pr) == PR_STATE_MERGED


def test_unknown_state_still_grounds_a_plan():
    """A PR we could not confirm is not the same fact as a PR we confirmed
    was abandoned. Deployments without a GitHub token must keep working."""
    usable, excluded = partition_pull_requests([{"status": "SOMETHING-ELSE"}])
    assert len(usable) == 1
    assert excluded == []


# ─── filtering ────────────────────────────────────────────────────────────────


def test_closed_unmerged_pr_is_dropped_before_the_prompt():
    """The SK-2563 defect at its narrowest: the abandoned PR's diff — and
    with it the ``deriveUserCalculatorOverridesBackfillDecision`` symbol —
    must not survive into what the model is shown."""
    dev_info = _dev_info(_merged_pr(), _sk2563_closed_pr())

    filtered, provenance = filter_development_info(dev_info)

    assert [pr["number"] for pr in filtered["pull_requests"]] == [1310]
    serialized = repr(filtered)
    assert "deriveUserCalculatorOverridesBackfillDecision" not in serialized
    assert "backfill-user-calculator-overrides" not in serialized
    assert provenance["excluded_pr_count"] == 1


def test_a_dropped_prs_branch_goes_with_it():
    """Otherwise the abandoned branch name is still quotable into a test
    step even though its diff is gone."""
    dev_info = _dev_info(
        _merged_pr(),
        _sk2563_closed_pr(),
        branches=["codex/SK-2563-reset-defaults", "codex/SK-2563-defaults-migration"],
    )

    filtered, _ = filter_development_info(dev_info)

    assert filtered["branches"] == ["codex/SK-2563-reset-defaults"]


def test_a_branch_shared_with_a_kept_pr_survives():
    kept = _merged_pr()
    dropped = _sk2563_closed_pr()
    dropped["source_branch"] = kept["source_branch"]
    dev_info = _dev_info(kept, dropped, branches=[kept["source_branch"]])

    filtered, _ = filter_development_info(dev_info)

    assert filtered["branches"] == [kept["source_branch"]]


def test_a_merged_only_ticket_is_passed_through_untouched():
    """No regression: the common case must be byte-identical."""
    dev_info = _dev_info(_merged_pr(), branches=["codex/SK-2563-reset-defaults"])

    filtered, provenance = filter_development_info(dev_info)

    assert filtered is dev_info
    assert provenance["excluded_pr_count"] == 0
    assert provenance["grounded_on_unmerged"] is False
    assert provenance["authoritative"] is True


# ─── provenance ───────────────────────────────────────────────────────────────


def test_provenance_records_numbers_shas_and_states():
    provenance = build_provenance(
        _dev_info(
            _merged_pr(),
            _open_pr(),
            _sk2563_closed_pr(),
            commits=[
                {
                    "message": "SK-2563 reset defaults",
                    "url": "https://github.com/skyslope/agent-calculator/commit/deadbee",
                }
            ],
        )
    )

    by_number = {e["number"]: e for e in provenance["pull_requests"]}
    assert by_number[1310]["state"] == PR_STATE_MERGED
    assert by_number[1310]["head_sha"] == "1" * 40
    assert by_number[1310]["used_as_grounding"] is True
    assert by_number[1401]["state"] == PR_STATE_OPEN
    assert by_number[1401]["used_as_grounding"] is True
    assert by_number[1303]["state"] == PR_STATE_CLOSED_UNMERGED
    assert by_number[1303]["used_as_grounding"] is False
    assert provenance["commit_shas"] == ["deadbee"]
    assert provenance["merged_pr_count"] == 1
    assert provenance["open_pr_count"] == 1
    assert provenance["excluded_pr_count"] == 1


def test_provenance_flags_a_plan_leaning_on_unmerged_code():
    assert build_provenance(_dev_info(_open_pr()))["grounded_on_unmerged"] is True
    assert build_provenance(_dev_info(_merged_pr()))["grounded_on_unmerged"] is False


def test_pr_number_falls_back_to_the_url():
    """Text-linked PRs arrive with no number field until GitHub enrichment
    runs, and enrichment is skipped when there's no token."""
    provenance = build_provenance(
        _dev_info(
            {
                "title": "pasted link",
                "status": "MERGED",
                "url": "https://github.com/skyslope/agent-calculator/pull/1303",
            }
        )
    )
    assert provenance["pull_requests"][0]["number"] == 1303


def test_merge_provenance_attributes_each_pr_to_its_ticket():
    merged = merge_provenance(
        [
            ("SK-2563", build_provenance(_dev_info(_sk2563_closed_pr()))),
            ("SK-2700", build_provenance(_dev_info(_open_pr()))),
        ]
    )

    assert merged["excluded_pr_count"] == 1
    assert merged["open_pr_count"] == 1
    assert merged["grounded_on_unmerged"] is True
    assert {e["number"]: e["ticket_key"] for e in merged["pull_requests"]} == {
        1303: "SK-2563",
        1401: "SK-2700",
    }
    assert set(merged["by_ticket"]) == {"SK-2563", "SK-2700"}


# ─── no source at all ─────────────────────────────────────────────────────────


def test_has_grounding_source_is_false_when_every_pr_was_excluded():
    provenance = build_provenance(_dev_info(_sk2563_closed_pr()))
    assert has_grounding_source(provenance) is False


def test_no_source_result_names_what_was_searched():
    provenance = build_provenance(_dev_info(_sk2563_closed_pr()))
    result = no_source_result("SK-2609", provenance)

    assert result["no_source"] is True
    assert result["happy_path"] == []
    assert result["edge_cases"] == []
    assert result["integration_tests"] == []
    searched = " ".join(result["searched"]).lower()
    assert "pull request" in searched
    assert "branch" in searched
    assert "commit" in searched
    # The abandoned PR is named, so a reader can tell "nothing usable was
    # found" from "we never looked".
    assert "skyslope/agent-calculator#1303" in " ".join(result["searched"])


# ─── quarantine ───────────────────────────────────────────────────────────────


def _case(title: str, **overrides) -> dict:
    case = {
        "title": title,
        "priority": "high",
        "steps": ["Open the defaults popover"],
        "expected": "The control renders",
        "surface": "web_ui",
        "credentials": "user session",
    }
    case.update(overrides)
    return case


def test_ungrounded_cases_leave_the_numbered_sections():
    """SK-2609's shape: a case whose UI element AND expected result were
    both un-sourced was numbered alongside verified cases and graded like
    them. It must land in the non-gradeable list instead."""
    plan = TestPlan(
        happy_path=[
            _case("Verified case", expected_verified=True),
            _case(
                "Personal / Brokerage / Group defaults control offers three options",
                needs_manual_verification=True,
                expected_verified=False,
            ),
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )

    quarantined = quarantine_ungrounded_cases(plan)

    assert [c["title"] for c in plan.happy_path] == ["Verified case"]
    assert len(quarantined) == 1
    assert quarantined[0]["needs_spec"] is True
    assert quarantined[0]["origin_section"] == "happy_path"
    assert quarantined[0]["needs_spec_reason"]


def test_a_partly_grounded_case_is_still_gradeable():
    """Only the intersection is speculation. A case that names a real
    control but guesses the outcome — or asserts verified behaviour through
    an unconfirmed element — is still worth a tester's time."""
    plan = TestPlan(
        happy_path=[_case("Real element, assumed result", expected_verified=False)],
        edge_cases=[
            _case("Verified result, unconfirmed element", needs_manual_verification=True)
        ],
        integration_tests=[],
        regression_checklist=[],
    )

    assert quarantine_ungrounded_cases(plan) == []
    assert len(plan.happy_path) == 1
    assert len(plan.edge_cases) == 1


def test_a_case_rescued_by_the_code_recheck_is_not_quarantined():
    """The code-grounding critic clears ``needs_manual_verification`` when
    it finds the behaviour in the repo. Quarantining runs after it, so that
    rescue has to hold."""
    plan = TestPlan(
        happy_path=[
            _case(
                "Empty synthesis does not overwrite cached audio",
                needs_manual_verification=False,
                expected_verified=False,
            )
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )

    assert quarantine_ungrounded_cases(plan) == []
    assert len(plan.happy_path) == 1


# ─── end-to-end through plan_service ──────────────────────────────────────────


class _StubLLM:
    """Records the development_info that reached the generator."""

    def __init__(self, captured: dict, plan: TestPlan | None = None):
        self.captured = captured
        self.plan = plan or TestPlan(
            happy_path=[], edge_cases=[], integration_tests=[], regression_checklist=[]
        )

    async def generate_test_plan(self, **kwargs):
        self.captured.update(kwargs)
        return self.plan

    async def verify_case_grounding(self, *a, **k):
        return []

    async def verify_code_grounding(self, *a, **k):
        return []

    async def verify_fix_scope(self, *a, **k):
        return []

    async def verify_surface_alignment(self, *a, **k):
        return []

    async def classify_deliverable(self, *a, **k):
        return None


WITH_ACS = "Acceptance Criteria:\n1. Defaults reset for internal testers\n"
# A ticket that gives the generator nothing to write from: no PR and no AC.
NO_ACS = "Reset the personal calculator defaults for internal testers."


def _request(
    dev_info: dict | None,
    ticket_key: str = "SK-2563",
    description: str = WITH_ACS,
    parent_key: str | None = None,
):
    from src.app.models import GenerateTestPlanRequest

    return GenerateTestPlanRequest(
        ticket_key=ticket_key,
        summary="Reset personal calculator defaults",
        description=description,
        issue_type="Story",
        development_info=dev_info,
        parent_info={"key": parent_key, "summary": "Parent"} if parent_key else None,
    )


async def _generate(
    dev_info,
    ticket_key="SK-2563",
    plan=None,
    description=WITH_ACS,
    parent_key=None,
):
    captured: dict = {}
    with (
        patch.object(
            plan_service.JiraClient,
            "download_image_as_base64",
            AsyncMock(return_value=None),
        ),
        patch.object(plan_service, "classify_deliverable", AsyncMock(return_value=None)),
    ):
        response = await plan_service.generate_single(
            _request(dev_info, ticket_key, description, parent_key),
            llm=_StubLLM(captured, plan),
        )
    return response, captured


@pytest.mark.asyncio
async def test_merged_pr_generates_a_plan_and_records_provenance():
    """The no-regression case: a ticket whose only PR is merged behaves
    exactly as before, now with provenance attached."""
    response, captured = await _generate(_dev_info(_merged_pr()))

    assert "no_source" not in response
    prs = captured["development_info"]["pull_requests"]
    assert [pr["number"] for pr in prs] == [1310]
    provenance = response["source_provenance"]
    assert provenance["merged_pr_count"] == 1
    assert provenance["grounded_on_unmerged"] is False
    assert provenance["pull_requests"][0]["head_sha"] == "1" * 40


@pytest.mark.asyncio
async def test_open_pr_generates_a_plan_labelled_as_unmerged():
    response, captured = await _generate(_dev_info(_open_pr()), ticket_key="SK-2700")

    assert "no_source" not in response
    assert [pr["number"] for pr in captured["development_info"]["pull_requests"]] == [
        1401
    ]
    provenance = response["source_provenance"]
    assert provenance["grounded_on_unmerged"] is True
    assert provenance["authoritative"] is False
    assert provenance["pull_requests"][0]["state"] == PR_STATE_OPEN


@pytest.mark.asyncio
async def test_sk2563_regenerated_never_sees_the_abandoned_pr():
    """The acceptance criterion, end to end: regenerating SK-2563 must not
    produce cases referencing the backfill script, because the generator is
    never shown it."""
    response, captured = await _generate(
        _dev_info(_merged_pr(), _sk2563_closed_pr())
    )

    shown = repr(captured["development_info"])
    assert "deriveUserCalculatorOverridesBackfillDecision" not in shown
    assert "backfill-user-calculator-overrides" not in shown
    excluded = [
        e for e in response["source_provenance"]["pull_requests"]
        if not e["used_as_grounding"]
    ]
    assert [e["number"] for e in excluded] == [1303]


@pytest.mark.asyncio
async def test_a_closed_unmerged_pr_alone_yields_no_plan():
    """SK-2563 if the abandoned PR had been its *only* PR, and it had no ACs.
    Nothing landed and nothing specifies the behaviour, so there is nothing
    to test."""
    response, captured = await _generate(
        _dev_info(_sk2563_closed_pr()), description=NO_ACS
    )

    assert response["no_source"] is True
    assert response["happy_path"] == []
    assert captured == {}, "the generator must not have been called at all"


@pytest.mark.asyncio
async def test_no_pr_at_all_reports_no_implementation_found():
    """SK-2609: ten cases of speculation before, a short report now."""
    response, captured = await _generate(
        None, ticket_key="SK-2609", description=NO_ACS
    )

    assert response["no_source"] is True
    assert response["ticket_key"] == "SK-2609"
    assert response["no_source_message"]
    assert response["searched"]
    assert response["happy_path"] == []
    assert response["edge_cases"] == []
    assert response["integration_tests"] == []
    assert response["regression_checklist"] == []
    assert captured == {}, "the generator must not have been called at all"


@pytest.mark.asyncio
async def test_a_no_source_skip_is_recorded_as_skipped_not_ok():
    """Run 959 (SK-2627) was stored as status ok, 660ms, no plan — the same
    record as a success. The skip has to say it is one."""
    with patch.object(plan_service.run_tracker, "skip", AsyncMock()) as skip:
        await _generate(None, ticket_key="SK-2609", description=NO_ACS)
    skip.assert_awaited_once()
    assert skip.await_args.kwargs["reason"] == "no_source"


# ─── no PR of its own, but ACs: write from the ACs (SK-2627) ─────────────────


def _sibling_lookup(result=None, exc=None):
    mock = AsyncMock(return_value=result or [], side_effect=exc)
    return patch.object(
        plan_service.JiraClient, "get_sibling_development_info", mock
    ), mock


@pytest.mark.asyncio
async def test_a_ticket_with_acs_and_no_pr_gets_a_plan_written_from_its_acs():
    lookup, _ = _sibling_lookup()
    with lookup:
        response, captured = await _generate(None, ticket_key="SK-2627")

    assert "no_source" not in response
    assert captured, "the generator must have been called"
    assert captured["development_info"]["spec_only"]["ac_count"] == 1
    notice = response["source_notice"]
    assert notice["mode"] == "spec_only"
    assert "written from its 1 acceptance criteria" in notice["message"]
    assert response["source_provenance"]["spec_only"]


@pytest.mark.asyncio
async def test_sibling_prs_are_borrowed_and_attributed():
    """SK-2627's email was built in SK-2630's PRs."""
    from src.app.models import DevelopmentInfo, PullRequest

    sibling = DevelopmentInfo(
        commits=[],
        pull_requests=[
            PullRequest(
                title="SK-2630 feat: add file-share invite email endpoint",
                status="MERGED",
                url="https://github.com/acme/user-service/pull/49",
                repository="acme/user-service",
                merged_at="2026-09-24T23:44:53Z",
                number=49,
            )
        ],
        branches=[],
    )
    lookup, mock = _sibling_lookup([("SK-2630", sibling, False), ("SK-2623", None, False)])
    with lookup:
        response, captured = await _generate(
            None, ticket_key="SK-2627", parent_key="SK-2620"
        )

    mock.assert_awaited_once_with("SK-2620", "SK-2627")
    prs = captured["development_info"]["pull_requests"]
    assert [(pr["number"], pr["borrowed_from"]) for pr in prs] == [(49, "SK-2630")]
    entry = response["source_provenance"]["pull_requests"][0]
    assert entry["ticket_key"] == "SK-2630" and entry["borrowed"] is True
    assert "SK-2630" in response["source_notice"]["message"]


@pytest.mark.asyncio
async def test_a_failed_sibling_lookup_is_not_reported_as_no_sibling_prs():
    lookup, _ = _sibling_lookup(exc=RuntimeError("Jira 503"))
    with lookup:
        response, captured = await _generate(
            None, ticket_key="SK-2627", parent_key="SK-2620"
        )

    assert captured, "a failed lookup still leaves the ACs to write from"
    message = response["source_notice"]["message"]
    assert "lookup" in message and "failed" in message
    assert "No other ticket" not in message


@pytest.mark.asyncio
async def test_ac_citing_cases_stay_gradeable_on_a_spec_only_plan():
    """Plan 551 filed SK-2627's sign-up case under needs-spec. On a plan
    written from ACs, the AC is the spec: a case citing one is gradeable."""
    plan = TestPlan(
        happy_path=[
            _case(
                "Sign Up For Forms lands in account creation",
                needs_manual_verification=True,
                expected_verified=False,
                covers_acs=["SK-2627-AC1"],
            ),
            _case(
                "Speculative control nobody specified",
                needs_manual_verification=True,
                expected_verified=False,
            ),
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    lookup, _ = _sibling_lookup()
    with lookup:
        response, _ = await _generate(None, ticket_key="SK-2627", plan=plan)

    assert [c["title"] for c in response["happy_path"]] == [
        "Sign Up For Forms lands in account creation"
    ]
    assert [c["title"] for c in response["needs_spec_cases"]] == [
        "Speculative control nobody specified"
    ]


def test_a_case_the_critic_says_exceeds_its_ac_is_still_quarantined():
    """Regenerated SK-2627 kept "Save fails — no success toast" gradeable
    because it cited AC1, though the critic said AC1 says nothing about
    failure. Citing an AC is not the same as the AC stating the outcome."""
    plan = TestPlan(
        happy_path=[
            _case(
                "Save fails — no success toast and no invitation email",
                needs_manual_verification=True,
                expected_verified=False,
                covers_acs=["SK-2627-AC1"],
                ac_grounding_disputed=True,
            )
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    moved = quarantine_ungrounded_cases(plan, spec_is_source=True)
    assert [c["title"] for c in moved] == [
        "Save fails — no success toast and no invitation email"
    ]


def test_an_ungrounded_destination_case_is_not_quarantined():
    """Outside spec-only mode too: the AC states where the button goes, so
    clicking it is a gradeable check even when the link target is not in
    any diff."""
    plan = TestPlan(
        happy_path=[
            _case(
                "Check It Out lands inside the shared file",
                needs_manual_verification=True,
                expected_verified=False,
                covers_acs=["SK-2627-AC7"],
                assertion_type="destination",
            )
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    assert quarantine_ungrounded_cases(plan) == []


@pytest.mark.asyncio
async def test_the_no_source_short_circuit_can_be_turned_off():
    """Deployments whose tickets routinely carry work the bot cannot see
    keep the pre-2026-09 behaviour via the setting."""
    with patch.object(plan_service.settings, "require_source_grounding", False):
        response, captured = await _generate(None, ticket_key="SK-2609")

    assert "no_source" not in response
    assert captured != {}


@pytest.mark.asyncio
async def test_ungrounded_cases_are_quarantined_out_of_the_response():
    """The full path: a case the pipeline can't trace to source reaches the
    caller in ``needs_spec_cases``, not in a numbered section."""
    plan = TestPlan(
        happy_path=[
            _case("Grounded case", expected_verified=True),
            _case(
                "Three-option defaults control",
                needs_manual_verification=True,
                expected_verified=False,
            ),
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )

    response, _ = await _generate(_dev_info(_merged_pr()), plan=plan)

    assert [c["title"] for c in response["happy_path"]] == ["Grounded case"]
    assert [c["title"] for c in response["needs_spec_cases"]] == [
        "Three-option defaults control"
    ]


@pytest.mark.asyncio
async def test_quarantined_cases_are_not_persisted_as_gradeable():
    """Persistence reads the sections after quarantining, so a needs-spec
    case never becomes a row a tester can mark pass or fail."""
    from src.app.services.test_plan_generator import flatten_cases_for_persistence

    plan = TestPlan(
        happy_path=[
            _case("Grounded case", expected_verified=True),
            _case(
                "Three-option defaults control",
                needs_manual_verification=True,
                expected_verified=False,
            ),
        ],
        edge_cases=[],
        integration_tests=[],
        regression_checklist=[],
    )
    quarantine_ungrounded_cases(plan)

    titles = [title for title, _body, _cat in flatten_cases_for_persistence(plan)]
    assert titles == ["Grounded case"]


# ─── how the prompt describes merge state ─────────────────────────────────────


class _PromptOnlyClient(LLMClient):
    """Concrete stand-in for the abstract base — the prompt builders are pure
    string assembly, so this exercises exactly what the real clients call.
    Mirrors the harness in tests/test_bounce_in_prompts.py."""

    async def generate_test_plan(self, *a, **k): ...
    async def generate_multi_ticket_test_plan(self, *a, **k): ...
    async def generate_bug_analysis(self, *a, **k): ...
    async def generate_multi_bug_analysis(self, *a, **k): ...
    async def summarize_ticket(self, *a, **k): ...
    async def summarize_batch(self, *a, **k): ...
    async def summarize_bounce_reason(self, *a, **k): ...
    async def summarize_pr_changes(self, *a, **k): ...


def _prompt_for(dev_info: dict) -> str:
    return _PromptOnlyClient()._build_prompt(
        "SK-2563", "A ticket", "body", {}, dev_info
    )


def test_prompt_marks_a_merged_pr_as_authoritative():
    prompt = _prompt_for(_dev_info(_merged_pr()))
    assert "MERGED — this code shipped; treat it as authoritative" in prompt
    assert "GROUNDED IN UNMERGED CODE" not in prompt


def test_prompt_marks_an_open_pr_as_not_yet_merged():
    """Requirement: open PRs still ground cases, but the model has to know
    the code can move before it lands."""
    prompt = _prompt_for(_dev_info(_open_pr()))
    assert "NOT YET MERGED" in prompt
    assert "GROUNDED IN UNMERGED CODE" in prompt
    assert "grounded_in_unmerged" in prompt


def test_prompt_never_says_declined_because_such_prs_never_reach_it():
    """Belt and braces: even if a DECLINED PR slipped past the filter, the
    old rendering that presented it as completed work is gone."""
    filtered, _ = filter_development_info(_dev_info(_sk2563_closed_pr()))
    prompt = _prompt_for(filtered)
    assert "DECLINED" not in prompt
    assert "backfill-user-calculator-overrides" not in prompt


# ─── the Jira comment that would have prevented SK-2563 ───────────────────────


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def _comment(text: str, author: str = "Dev Person") -> dict:
    return {
        "author": {"displayName": author, "accountId": "acct-1"},
        "body": _adf(text),
        "created": "2026-09-08T20:00:00.000+0000",
    }


def test_a_scope_decision_comment_outranks_testing_chatter():
    """SK-2563's real prevention: the author said in a Jira comment, the day
    before generation, that the migration was being abandoned. Five
    testing-keyword comments about that same migration used to fill the
    budget and push the decision out."""
    from src.app.jira_client import JiraClient

    chatter = [
        _comment(f"Edge case {i}: verify the backfill handles null defaults")
        for i in range(6)
    ]
    decision = _comment(
        "Closing this PR because the legacy personal defaults only belong to "
        "internal testers. We will reset userCalculatorFieldDefaults instead "
        "of inferring them."
    )

    selected = JiraClient._filter_testing_comments(
        object.__new__(JiraClient), chatter + [decision]
    )

    bodies = [c.body for c in selected]
    assert any("Closing this PR" in b for b in bodies), (
        "the abandonment decision must survive the comment filter"
    )
    assert bodies[0].startswith("Closing this PR"), (
        "and must be ranked above ordinary testing chatter"
    )


def test_prompt_tells_the_model_it_is_writing_from_the_acs():
    prompt = _prompt_for(
        {
            "pull_requests": [{**_merged_pr(), "borrowed_from": "SK-2630"}],
            "spec_only": {"ac_count": 3, "borrowed_from": ["SK-2630"]},
        }
    )
    assert "NO PULL REQUEST OF THIS TICKET'S OWN" in prompt
    assert "cannot be verified from source" in prompt
    assert "Belongs to sibling ticket SK-2630" in prompt
