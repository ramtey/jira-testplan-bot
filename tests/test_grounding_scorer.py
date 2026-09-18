"""The pure parts of the grounding scorer, and the distinctions it must keep.

The scorer's whole value is that its three verdicts mean different things, and
that a lookup which failed is never counted as evidence about the plan. Both
are easy to lose in an aggregate, so they are pinned here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.label_grounding import _kappa
from evals.score_grounding import _parse_locator, plan_repos, render_snippet


class TestParsingACitation:
    @pytest.mark.parametrize("source,expected", [
        ("src/a.ts:44", ("src/a.ts", 44, 44)),
        ("src/a.ts:90-98", ("src/a.ts", 90, 98)),
        ("apps/expo/src/app/(calculation)/x.tsx:12", ("apps/expo/src/app/(calculation)/x.tsx", 12, 12)),
        ("packages/engine/src/b.ts:485-489 (monthlyHOA expression)", ("packages/engine/src/b.ts", 485, 489)),
    ])
    def test_it_reads_path_and_lines(self, source, expected):
        assert _parse_locator(source) == expected

    @pytest.mark.parametrize("source", [
        "src/containers/base-field.tsx (externalSource)",  # no line at all
        "", None, "just some prose",
    ])
    def test_an_unciteable_source_is_not_guessed_at(self, source):
        """Guessing a line would manufacture evidence for a claim that gave
        none — the scorer records these as unparseable instead."""
        assert _parse_locator(source) is None


class TestChoosingTheCommit:
    def test_it_takes_repo_and_sha_from_provenance(self):
        plan = {"source_provenance": {"pull_requests": [
            {"repository": "o/r", "head_sha": "abc"},
            {"repository": "o/r2", "head_sha": "def"},
        ]}}
        assert plan_repos(plan) == [("o/r", "abc"), ("o/r2", "def")]

    def test_duplicate_prs_on_one_repo_are_collapsed(self):
        plan = {"source_provenance": {"pull_requests": [
            {"repository": "o/r", "head_sha": "abc"},
            {"repository": "o/r", "head_sha": "abc"},
        ]}}
        assert plan_repos(plan) == [("o/r", "abc")]

    def test_a_pr_with_no_sha_cannot_pin_a_commit(self):
        plan = {"source_provenance": {"pull_requests": [{"repository": "o/r"}]}}
        assert plan_repos(plan) == []


class TestTheSnippetShownToTheJudge:
    def test_the_cited_lines_are_marked(self):
        out = render_snippet([f"line{i}" for i in range(1, 101)], 50, 52)
        marked = [l for l in out.splitlines() if l.startswith(">>>")]
        assert len(marked) == 3
        assert "line50" in marked[0] and "line52" in marked[-1]

    def test_it_does_not_run_past_the_start_or_end_of_the_file(self):
        out = render_snippet(["a", "b", "c"], 1, 1)
        assert len(out.splitlines()) == 3, "a 3-line file has 3 lines of context"

    def test_line_numbers_are_shown_so_a_human_can_check(self):
        out = render_snippet(["x", "y"], 2, 2)
        assert "2 | y" in out


class TestAgreement:
    def test_perfect_agreement_is_one(self):
        assert _kappa([("SUPPORTS", "SUPPORTS")] * 10) == 1.0

    def test_agreeing_only_as_often_as_chance_is_zero(self):
        """Both always say SUPPORTS: 100% raw agreement, no information."""
        pairs = [("SUPPORTS", "SUPPORTS")] * 10
        assert _kappa(pairs) == 1.0
        mixed = [("SUPPORTS", "SUPPORTS")] * 5 + [("SILENT", "SUPPORTS")] * 5
        assert _kappa(mixed) == pytest.approx(0.0, abs=1e-9)

    def test_disagreement_goes_negative(self):
        pairs = [("SUPPORTS", "SILENT")] * 5 + [("SILENT", "SUPPORTS")] * 5
        assert _kappa(pairs) < 0

    def test_no_pairs_is_not_an_answer(self):
        assert _kappa([]) is None
