"""How a PR's diff is cut down to fit in a prompt.

The old rule was "walk the files in whatever order they arrived, give each up
to 4000 characters, stop at 16000". On a small PR that is fine. On a large one
it spends the whole budget on the first few files and the model never sees the
rest — and it has no way to know that, so it reports the behaviour as
unconfirmable and the pipeline quarantines the cases built on it.

SK-2330 is the worked example. PR #28 on forms-android changed 30 Kotlin
files, 67,019 characters of diff. The budget admitted 16,000 — 24% — and path
order spent it on `data/`, `di/` and `domain/`. All eight `presentation/`
files were cut, which is where the Individual/Entity toggle, the contact
details screen and the save path live. The model correctly said it could not
confirm any UI element; 24 of the plan's 26 cases were quarantined as
ungradeable, and the two defects QA later filed were in exactly the code it
never saw.

Two changes, and the second matters more:

**Rank.** Files that render something come first. A test plan is a document
about observable behaviour, so the screen is worth more than the DI module. A
backend-only PR has no such files and keeps its original order.

**Spread.** Every file gets a slice before any file gets a second helping.
Seeing 600 characters of each of 30 files beats seeing all of six: the first
tells the model the toggle exists and roughly what it does, which is the
difference between a gradeable case and a quarantined one. Leftover budget is
then handed back out in rank order, so a small PR still shows whole diffs.
"""

from __future__ import annotations

#: Directory names that mean "this file draws something". Matched as path
#: segments so `presentation/` hits and `representation.kt` does not.
_UI_SEGMENTS = frozenset({
    "presentation", "screens", "screen", "components", "component",
    "views", "view", "pages", "page", "ui", "widgets", "containers", "layouts",
})

#: Extensions that are user interface by construction.
_UI_EXTENSIONS = (".tsx", ".jsx", ".vue", ".svelte", ".xml", ".storyboard", ".xib")

#: Floor for one file's slice. Below roughly this, a diff hunk is too small to
#: tell the model what a control is called or what it does.
DEFAULT_MIN_SLICE = 600


def is_ui_file(filename: str) -> bool:
    path = (filename or "").lower()
    if path.endswith(_UI_EXTENSIONS):
        return True
    return any(seg in _UI_SEGMENTS for seg in path.split("/"))


def rank_files(files: list[dict]) -> list[dict]:
    """UI first, everything else after, original order kept inside each group."""
    ui, rest = [], []
    for f in files:
        (ui if is_ui_file(f.get("filename") or "") else rest).append(f)
    return ui + rest


def allocate_patch_budget(
    files: list[dict],
    *,
    total_budget: int,
    per_file_cap: int,
    min_slice: int = DEFAULT_MIN_SLICE,
) -> list[tuple[str, str, bool]]:
    """Decide how much of each file's patch to show.

    Returns `(filename, text, truncated)` in the order to render. Files with no
    patch, and files that got no budget at all, are left out.
    """
    with_patch = [f for f in rank_files(files) if f.get("patch")]
    if not with_patch or total_budget <= 0:
        return []

    lengths = [len(f["patch"]) for f in with_patch]
    takes = [0] * len(with_patch)
    budget = total_budget

    # Breadth first: an even share, never below the floor and never above the
    # per-file cap. On a small PR the share exceeds every patch and this pass
    # alone shows all of them whole.
    share = max(min_slice, min(per_file_cap, total_budget // len(with_patch)))
    for i, length in enumerate(lengths):
        if budget <= 0:
            break
        take = min(length, share, budget)
        takes[i] = take
        budget -= take

    # Then depth, in rank order, so leftover budget goes to the files most
    # worth reading in full.
    for i, length in enumerate(lengths):
        if budget <= 0:
            break
        extra = min(length, per_file_cap) - takes[i]
        if extra <= 0:
            continue
        add = min(extra, budget)
        takes[i] += add
        budget -= add

    return [
        (with_patch[i]["filename"], with_patch[i]["patch"][:takes[i]], takes[i] < lengths[i])
        for i in range(len(with_patch))
        if takes[i] > 0
    ]


def omitted_note(files: list[dict], shown: list[tuple[str, str, bool]]) -> str | None:
    """What the model did not get to see, so it can say so rather than guess.

    A file whose diff was cut is not a file that changed nothing, and the
    difference is the whole reason this module exists.
    """
    with_patch = [f for f in files if f.get("patch")]
    shown_names = {name for name, _, _ in shown}
    missing = [f["filename"] for f in with_patch if f["filename"] not in shown_names]
    truncated = [name for name, _, was_cut in shown if was_cut]
    if not missing and not truncated:
        return None
    bits = []
    if truncated:
        bits.append(f"{len(truncated)} file(s) shown only in part")
    if missing:
        bits.append(f"{len(missing)} file(s) not shown at all: "
                    + ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""))
    return (
        "NOTE: this diff was too large to include whole — "
        + "; ".join(bits)
        + ". Code you cannot see here may still exist. Treat an element you "
          "cannot find as unconfirmed, not as absent."
    )


def render_full_files(files: list[dict], *, total_budget: int, per_file_cap: int = 60_000) -> str:
    """The changed files in full, line-numbered, within `total_budget` chars.

    Line numbers are the point: they let a case cite `Handler.cs:74` as the
    line that returns its 403, which a diff hunk's relative offsets cannot.
    Returns "" when no file carries `full_content`.
    """
    numbered = [
        {"filename": f["filename"],
         "patch": "\n".join(f"{n:5d}| {line}"
                            for n, line in enumerate(f["full_content"].splitlines(), 1))}
        for f in files if f.get("full_content")
    ]
    shown = allocate_patch_budget(numbered, total_budget=total_budget, per_file_cap=per_file_cap)
    if not shown:
        return ""
    out = []
    for name, text, was_cut in shown:
        if was_cut:
            text = text.rsplit("\n", 1)[0] + "\n  … (file continues; the rest was over budget)"
        out.append(f"--- {name} (whole file after this PR) ---\n{text}")
    note = omitted_note(numbered, shown)
    if note:
        out.append(note)
    return "\n\n".join(out)
