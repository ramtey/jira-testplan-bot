"""`expected_verified: true` is a promise; this audits whether it could hold.

Measured on the 34 replayed plans (2026-09-16): 101 of 362 verified citations
could not be what they claimed — 50 named a file with no line, 50 cited line 1
(the import region), 5 named several sources where the schema asks for the
single primary `<file>:<line>`. A tester saw none of that; they saw
"Expected verified against:" and a path, which the schema itself describes as
telling the reviewer to stop checking.

The constraint that shapes the whole module is in TestItNeverMovesACase: 40 of
those cases also carry `needs_manual_verification`, so flipping
`expected_verified` to False would satisfy `_is_ungrounded` and quarantine
them out of their section. Section counts are the progress-key fingerprint, so
that would orphan QA's marks on every plan in flight.
"""
import pytest

from src.app.citation_integrity import flag_unconfirmed_citations, inspect_citation
from src.app.services.test_plan_generator import quarantine_ungrounded_cases


class TestAWellFormedCitationIsLeftAlone:
    @pytest.mark.parametrize("source", [
        "src/hooks/use-net-sheet-promo.ts:44",
        "apps/expo/src/hooks/auth/usePostLoginSetup.ts:90-98",
        "packages/engine/src/calculators/buyerNetSheet.ts:485-489 (monthlyHOA expression)",
        "src/middleware/auth.ts:64",
    ])
    def test_single_file_and_line_passes(self, source):
        assert inspect_citation(source) == []

    @pytest.mark.parametrize("source", [
        "apps/expo/src/app/(calculation)/ai-assist-comparison.tsx:140",
        "apps/expo/src/app/(calculation)/share-flow/preview.tsx:120-140",
        "apps/expo/src/app/(tabs)/index.tsx:22",
    ])
    def test_an_expo_route_group_is_part_of_the_path(self, source):
        """Caught by rendering the output rather than by a test: stripping
        every `(...)` ate the route group out of `app/(calculation)/x.tsx`,
        leaving a fragment with no colon, which was then reported as "cites a
        file but no line number". 20 of the 34 plans' paths look like this —
        a false flag inside the guard against false flags."""
        assert inspect_citation(source) == []

    def test_a_parenthetical_is_not_a_second_source(self):
        """Asides routinely contain commas and the word "and"; splitting on
        them naively reported single citations as multi-source."""
        assert inspect_citation(
            'apps/expo/src/components/Header.tsx:12 (pointerEvents="none", and overflow)'
        ) == []


class TestItNamesWhatIsWrong:
    def test_no_line_number(self):
        r = inspect_citation("src/containers/net-sheet/base-field.tsx (`externalSource`)")
        assert r == ["cites a file but no line number"]

    def test_line_one(self):
        r = inspect_citation("apps/expo/src/hooks/share/constants.ts:1")
        assert len(r) == 1 and "line 1" in r[0]

    def test_several_sources(self):
        r = inspect_citation("apps/expo/src/a.tsx:12 and apps/expo/src/b.tsx:30")
        assert len(r) == 1 and "cites 2 sources" in r[0]

    @pytest.mark.parametrize("source", ["", None, "   "])
    def test_no_citation_at_all(self, source):
        assert inspect_citation(source) == ["claims a verified expected result but cites no source"]


class TestItAuditsOnlyThePromise:
    """An honest "I assumed this" owes no citation."""

    def test_an_unverified_case_is_not_flagged(self):
        plan = _plan(happy_path=[{"title": "t", "expected_verified": False}])
        assert flag_unconfirmed_citations(plan) == []
        assert "expected_source_unconfirmed" not in plan.happy_path[0]

    def test_a_case_with_no_verification_field_is_not_flagged(self):
        plan = _plan(happy_path=[{"title": "t"}])
        assert flag_unconfirmed_citations(plan) == []

    def test_a_verified_case_with_a_bad_citation_is_flagged(self):
        plan = _plan(happy_path=[
            {"title": "t", "expected_verified": True, "expected_source": "a/b.ts:1"},
        ])
        flagged = flag_unconfirmed_citations(plan)
        assert len(flagged) == 1
        case = plan.happy_path[0]
        assert case["expected_source_unconfirmed"] is True
        assert "line 1" in case["expected_source_unconfirmed_reason"]


class TestItNeverMovesACase:
    """The measured constraint. 40 of the 113 flagged cases also carry
    needs_manual_verification; downgrading expected_verified would quarantine
    every one of them and change the section counts the progress key hashes."""

    def test_expected_verified_is_left_exactly_as_generated(self):
        plan = _plan(happy_path=[{
            "title": "t", "expected_verified": True, "expected_source": "a/b.ts:1",
            "needs_manual_verification": True,
        }])
        flag_unconfirmed_citations(plan)
        assert plan.happy_path[0]["expected_verified"] is True

    def test_a_flagged_case_still_survives_quarantining(self):
        plan = _plan(happy_path=[{
            "title": "survives", "expected_verified": True,
            "expected_source": "a/b.ts:1", "needs_manual_verification": True,
        }])
        flag_unconfirmed_citations(plan)
        quarantined = quarantine_ungrounded_cases(plan)
        assert quarantined == [], "flagging must not make a case ungroundable"
        assert len(plan.happy_path) == 1

    def test_section_counts_are_unchanged(self):
        plan = _plan(
            happy_path=[{"title": "a", "expected_verified": True, "expected_source": "x.ts:1"}],
            edge_cases=[{"title": "b", "expected_verified": True, "expected_source": "y.ts"}],
            integration_tests=[{"title": "c", "expected_verified": False}],
        )
        before = (len(plan.happy_path), len(plan.edge_cases), len(plan.integration_tests))
        flag_unconfirmed_citations(plan)
        after = (len(plan.happy_path), len(plan.edge_cases), len(plan.integration_tests))
        assert before == after


class TestEverySectionWithMetadataIsCovered:
    def test_quarantined_cases_are_audited_too(self):
        plan = _plan(needs_spec_cases=[
            {"title": "q", "expected_verified": True, "expected_source": "z.ts:1"},
        ])
        assert len(flag_unconfirmed_citations(plan)) == 1

    def test_regression_strings_are_skipped_without_error(self):
        """Bare strings carry no metadata — the same shape asymmetry that
        exempts the checklist from every other grounding guard."""
        plan = _plan(regression_checklist=["a bare string", "another"])
        assert flag_unconfirmed_citations(plan) == []


class _Plan:
    def __init__(self, **sections):
        for name in ("happy_path", "edge_cases", "integration_tests",
                     "needs_spec_cases", "regression_checklist"):
            setattr(self, name, sections.get(name, []))


def _plan(**sections):
    return _Plan(**sections)
