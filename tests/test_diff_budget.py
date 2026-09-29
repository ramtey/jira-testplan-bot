"""How a large PR's diff is cut down to fit a prompt.

SK-2330 is the case that produced this module. PR #28 on forms-android changed
30 Kotlin files, 67,019 characters of diff. The budget admitted 16,000, path
order spent it on data/ di/ domain/, and all eight presentation/ files were
cut — the screen, the Individual/Entity toggle and the save path. The model
said, correctly, that it could not confirm any UI element; 24 of the plan's 26
cases were then quarantined as ungradeable, and the two defects QA filed five
days later were in the code it never saw.

The fix has two halves and the second is the load-bearing one: rank the files
that draw something first, and give every file a slice before any file gets a
second helping.
"""
import pytest

from src.app.diff_budget import (
    DEFAULT_MIN_SLICE, allocate_patch_budget, is_ui_file, omitted_note, rank_files,
)


def _f(name, size=1000):
    return {"filename": name, "patch": "x" * size}


class TestSpottingTheFilesThatDrawSomething:
    @pytest.mark.parametrize("name", [
        "app/src/main/java/com/x/presentation/screens/home/Screen.kt",
        "apps/expo/src/components/share/Sheet.tsx",
        "src/containers/file/net-sheet/field.tsx",
        "app/src/main/res/layout/activity_main.xml",
        "Sources/Views/ProfileView.swift",
    ])
    def test_ui_files_are_recognised(self, name):
        assert is_ui_file(name)

    @pytest.mark.parametrize("name", [
        "app/src/main/java/com/x/data/repositories/ContactsRepositoryImpl.kt",
        "app/src/main/java/com/x/di/AppModule.kt",
        "apps/server/src/api/router/handler.ts",
    ])
    def test_plumbing_is_not(self, name):
        assert not is_ui_file(name)

    def test_a_segment_must_be_whole(self):
        """`representation.kt` is not `presentation/`."""
        assert not is_ui_file("src/models/representation.kt")

    def test_ranking_keeps_original_order_inside_each_group(self):
        files = [_f("a/data/one.kt"), _f("b/ui/two.kt"), _f("c/data/three.kt"),
                 _f("d/screens/four.kt")]
        names = [f["filename"] for f in rank_files(files)]
        assert names == ["b/ui/two.kt", "d/screens/four.kt",
                         "a/data/one.kt", "c/data/three.kt"]

    def test_a_backend_only_pr_is_left_alone(self):
        files = [_f("api/a.ts"), _f("api/b.ts"), _f("api/c.ts")]
        assert [f["filename"] for f in rank_files(files)] == \
               ["api/a.ts", "api/b.ts", "api/c.ts"]


class TestEveryFileGetsSeen:
    """The heart of it. Before, six files consumed the budget and twenty-four
    were invisible — and an invisible file reads exactly like a file that
    changed nothing."""

    def test_a_large_pr_shows_something_from_almost_every_file(self):
        files = [_f(f"data/f{i}.kt", 2500) for i in range(30)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert len(out) >= 26, f"only {len(out)} of 30 files reached the prompt"

    def test_the_old_greedy_rule_would_have_shown_far_fewer(self):
        files = [_f(f"data/f{i}.kt", 2500) for i in range(30)]
        greedy, used = 0, 0
        for f in files:
            if used >= 16000:
                break
            used += min(len(f["patch"]), 4000)
            greedy += 1
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert len(out) > greedy * 2

    def test_no_slice_is_uselessly_small(self):
        files = [_f(f"data/f{i}.kt", 5000) for i in range(20)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert all(len(text) >= DEFAULT_MIN_SLICE for _, text, _ in out)

    def test_the_budget_is_never_exceeded(self):
        files = [_f(f"data/f{i}.kt", 9000) for i in range(40)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert sum(len(t) for _, t, _ in out) <= 16000

    def test_ui_files_are_served_first_when_the_budget_binds(self):
        files = [_f(f"data/f{i}.kt", 4000) for i in range(20)]
        files.append(_f("presentation/screens/ContactDetails.kt", 4000))
        out = allocate_patch_budget(files, total_budget=8000, per_file_cap=4000)
        assert out[0][0] == "presentation/screens/ContactDetails.kt"


class TestSmallPullRequestsAreUnaffected:
    def test_everything_fits_and_nothing_is_marked_truncated(self):
        files = [_f("a/ui/one.tsx", 500), _f("b/data/two.ts", 800)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert len(out) == 2
        assert all(not was_cut for _, _, was_cut in out)
        assert sum(len(t) for _, t, _ in out) == 1300

    def test_leftover_budget_is_handed_back_out(self):
        """One big file and one small: the small one fits whole, and the big
        one should still get everything the cap allows."""
        files = [_f("a/data/big.kt", 9000), _f("b/data/small.kt", 200)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        by = {n: t for n, t, _ in out}
        assert len(by["b/data/small.kt"]) == 200
        assert len(by["a/data/big.kt"]) == 4000

    def test_files_without_a_patch_are_skipped(self):
        files = [{"filename": "a.kt"}, _f("b.kt", 100)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert [n for n, _, _ in out] == ["b.kt"]


class TestItSaysWhatItCouldNotShow:
    """A file cut from the prompt is not a file that changed nothing, and the
    model has no way to tell the difference unless it is told."""

    def test_omissions_are_named(self):
        """Enough files that the floor cannot cover them all — three fit at
        600 characters each, the rest get nothing and must be named."""
        files = [_f(f"data/f{i}.kt", 9000) for i in range(20)]
        out = allocate_patch_budget(files, total_budget=2000, per_file_cap=4000)
        assert len(out) < len(files)
        note = omitted_note(files, out)
        assert note and "not shown at all" in note
        assert "unconfirmed, not as absent" in note

    def test_partial_files_are_called_out(self):
        files = [_f("a/data/big.kt", 9000)]
        out = allocate_patch_budget(files, total_budget=4000, per_file_cap=4000)
        note = omitted_note(files, out)
        assert note and "only in part" in note

    def test_a_fully_shown_diff_says_nothing(self):
        files = [_f("a/ui/one.tsx", 500)]
        out = allocate_patch_budget(files, total_budget=16000, per_file_cap=4000)
        assert omitted_note(files, out) is None
