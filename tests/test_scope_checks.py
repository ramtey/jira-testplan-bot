"""Scope rules G, H and J in code (src/app/scope_checks.py).

The shapes are SK-2822's plan 620, sanitized: the repo is public and the
sibling repositories it names still carry the flaw the ticket fixed.
"""

from types import SimpleNamespace

from src.app.scope_checks import (
    PLATFORM_MARKER,
    PRE_EXISTING_LABEL,
    changed_repos,
    flag_out_of_scope_cases,
    flag_platform_assumptions,
    platform_traps,
    run_scope_checks,
)

DEV = {"pull_requests": [{"url": "https://github.com/acme/widgets-db/pull/4"}]}


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [],
            "security_negative_tests": [], "regression_checklist": []}
    base.update(sections)
    return SimpleNamespace(**base)


def test_filename_case_without_a_platform_is_marked():
    case = {
        "title": "An invalid MODE value exits 1",
        "expected": "Both runs exit 1, because the committed filenames are "
                    "case-sensitive (conf/app-Integ.conf).",
    }
    plan = _plan(edge_cases=[case])
    flagged = flag_platform_assumptions(plan)
    assert flagged == [{"location": "edge_cases[0]", "traps": ["filename case"]}]
    assert case["expected"].startswith(PLATFORM_MARKER)


def test_naming_the_platform_anywhere_in_the_case_scopes_it():
    case = {
        "title": "Lowercase MODE on Linux CI",
        "expected": "Exits 1: the conf filenames are case-sensitive.",
    }
    plan = _plan(edge_cases=[case])
    assert flag_platform_assumptions(plan) == []
    assert not case["expected"].startswith(PLATFORM_MARKER)


def test_app_level_case_insensitivity_is_not_a_platform_trap():
    # "case-insensitive" about the app's own matching says nothing about the host.
    assert platform_traps("The email lookup is case-insensitive and returns one user") == []


def test_gnu_flags_and_executable_bit_are_traps():
    assert platform_traps("Running sed -i on the conf rewrites it") == ["GNU vs BSD flags"]
    assert platform_traps("The script is not executable after checkout") == ["executable bit"]


def test_regression_line_is_marked_after_its_priority_emoji():
    plan = _plan(regression_checklist=[
        "🟢 docs/threat-model.md matches the case-sensitive conf/app-<Mode>.conf filenames",
        "🟢 Off the VPN the script fails with the DNS message",
    ])
    flagged = flag_platform_assumptions(plan)
    assert [f["location"] for f in flagged] == ["regression_checklist[0]"]
    assert plan.regression_checklist[0].startswith("🟢 " + PLATFORM_MARKER)
    assert len(plan.regression_checklist) == 2  # never adds or drops a line


def test_marking_is_idempotent():
    case = {"title": "t", "expected": "Fails: filenames are case-sensitive in conf/"}
    plan = _plan(edge_cases=[case])
    flag_platform_assumptions(plan)
    once = case["expected"]
    flag_platform_assumptions(plan)
    assert case["expected"] == once


def test_pre_existing_flag_becomes_visible_text():
    case = {"title": "Guard accepts a matching conf", "pre_existing": True,
            "expected": "The guard at run.sh:4 accepts it on macOS."}
    plan = _plan(edge_cases=[case])
    assert run_scope_checks(plan, [DEV])["pre_existing_labelled"] == 1
    assert case["expected"].startswith(PRE_EXISTING_LABEL)
    assert run_scope_checks(plan, [DEV])["pre_existing_labelled"] == 0


def test_case_resting_on_an_unchanged_sibling_repo_is_questioned():
    sibling = {
        "title": "Sibling repo parity",
        "expected": "Mark FAILED: the sibling still uses the insecure factory.",
        "expected_source": "acme/widgets-db-fork:conf/app-Integ.conf:1-5",
        "seam": {"verified": False},
    }
    own = {"title": "Own config", "expected": "verify-full is set",
           "expected_source": "acme/widgets-db:conf/app-Prod.conf:2"}
    bare = {"title": "Bare path", "expected": "x", "expected_source": "src/auth.ts:64"}
    plan = _plan(integration_tests=[sibling, own, bare])
    flagged = flag_out_of_scope_cases(plan, changed_repos([DEV]))
    assert flagged == [{"location": "integration_tests[0]", "repo": "acme/widgets-db-fork"}]
    assert sibling["expected"].startswith("Check scope")
    assert own["expected"] == "verify-full is set"
    assert bare["expected"] == "x"


def test_a_verified_seam_in_another_repo_is_a_real_case():
    consumer = {"title": "Consumer reads the new field", "expected": "ok",
                "expected_source": "acme/web:src/api.ts:10", "seam": {"verified": True}}
    plan = _plan(integration_tests=[consumer])
    assert flag_out_of_scope_cases(plan, {"acme/widgets-db"}) == []


def test_no_pr_repos_means_no_scope_verdict():
    case = {"title": "t", "expected": "x", "expected_source": "acme/other:a.py:3"}
    assert flag_out_of_scope_cases(_plan(edge_cases=[case]), set()) == []


def test_a_case_that_expects_to_fail_on_a_scope_gap_is_not_counted():
    # 622's new edge_cases:8: "Expect this case to FAIL against the current code — it is
    # a scope gap, recorded in risks_and_gaps".
    from src.app.scope_checks import KNOWN_FAILURE_MARKER
    case = {"title": "Second unhardened login script",
            "expected": "It currently does not. Expect this case to FAIL — it is a scope gap."}
    plan = _plan(edge_cases=[case])
    scope = run_scope_checks(plan, [DEV])
    assert scope["out_of_scope_suspects"] == [{"location": "edge_cases[0]", "repo": None}]
    assert case["expected"].startswith(KNOWN_FAILURE_MARKER)
    run_scope_checks(plan, [DEV])
    assert case["expected"].count(KNOWN_FAILURE_MARKER) == 1


def test_lowercase_without_the_word_case_sensitive_is_still_a_trap():
    # 623's regression_checklist:7.
    assert platform_traps("lowercase `MODE=integ ./run.sh info` exits non-zero") == ["filename case"]


def test_a_sibling_finding_that_is_explicitly_not_counted_is_not_marked():
    # 623's integration_tests:4 followed rule J; the first regex marked it anyway.
    from src.app.scope_checks import flag_known_failures
    case = {"title": "Rerun clears the fingerprint", "expected":
            "Absent for this repo. If it appears for a sibling, that is the separate scope "
            "gap recorded in risks_and_gaps and is NOT a failure of this change."}
    assert flag_known_failures(_plan(integration_tests=[case])) == []


import pytest  # noqa: E402

from src.app.scope_checks import run_platform_critic  # noqa: E402


class _Critic:
    def __init__(self, answer):
        self.answer, self.seen = answer, None

    async def rewrite_platform_assumptions(self, items):
        self.seen = items
        return self.answer


def _guard_plan():
    case = {"title": "Invalid MODE exits 1", "steps": ["Run MODE=integ ./run.sh"],
            "expected": "Exits 1 because the conf filenames are case-sensitive."}
    dev = {"pull_requests": [{"url": "https://github.com/acme/widgets-db/pull/4",
                              "files_changed": [{"filename": "run.sh", "patch": "+[ -f ./conf/app-$MODE.conf ]"}]}]}
    return _plan(edge_cases=[case], regression_checklist=["🟡 lowercase MODE=integ ./run.sh exits 1"]), case, dev


@pytest.mark.asyncio
async def test_platform_critic_rewrites_in_place_and_sees_the_diff():
    plan, case, dev = _guard_plan()
    critic = _Critic({
        "edge_cases:0": {"id": "edge_cases:0", "action": "rewrite",
                         "text": "On macOS the -f check finds app-Integ.conf and the run proceeds; on Linux CI it exits 1."},
        "regression_checklist:0": {"id": "regression_checklist:0", "action": "rewrite",
                                   "text": "🟡 lowercase MODE=integ: record the result and your platform (macOS proceeds, Linux exits 1)"},
    })
    rewritten, gap = await run_platform_critic(critic, plan, [dev])
    assert (rewritten, gap) == (["edge_cases:0", "regression_checklist:0"], None)
    assert critic.seen[0]["code"] == {"run.sh": "+[ -f ./conf/app-$MODE.conf ]"}
    assert case["expected"].startswith("On macOS")
    assert len(plan.edge_cases) == 1 and len(plan.regression_checklist) == 1
    assert flag_platform_assumptions(plan) == []  # rewritten text names a platform


@pytest.mark.asyncio
async def test_a_rewrite_that_still_names_no_platform_is_refused_and_marked():
    plan, case, dev = _guard_plan()
    critic = _Critic({"edge_cases:0": {"id": "edge_cases:0", "action": "rewrite",
                                       "text": "Exits 1 because filenames are case-sensitive."}})
    rewritten, _gap = await run_platform_critic(critic, plan, [dev])
    assert "edge_cases:0" not in rewritten
    flag_platform_assumptions(plan)
    assert case["expected"].startswith(PLATFORM_MARKER)


@pytest.mark.asyncio
async def test_a_critic_that_could_not_run_says_so():
    plan, _case, dev = _guard_plan()
    rewritten, gap = await run_platform_critic(_Critic(None), plan, [dev])
    assert rewritten == [] and "could not run" in gap


def test_a_recording_step_that_is_not_counted_does_not_mark_the_case():
    # The diagnostic run's cleanDisabled case: a real case whose step 5 is a
    # recording step "filed in risks_and_gaps, not counted against this ticket".
    from src.app.scope_checks import flag_known_failures
    case = {"title": "cleanDisabled blocks clean on each remote environment", "expected":
            "clean is refused on each environment. Step 5 is a recording step, not a "
            "pass/fail: the missing global setting is filed in risks_and_gaps, not counted "
            "against this ticket."}
    assert flag_known_failures(_plan(edge_cases=[case])) == []

