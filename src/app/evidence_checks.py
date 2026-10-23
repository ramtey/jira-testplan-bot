"""What a plan has to be built from, and the checks that hold it to that.

SK-2826's first plan (plan 608) read well and was wrong in six ways, none of
them specific to that ticket:

1. Testing notes on the ticket — deploy check, receiver setup, trigger steps,
   four concrete cases, a Loki query, rollback — never reached a case. Comments
   were cut to five by keyword and carried no id, so nothing could cite one.
2. It sent the tester to "a non-prod deployment" while integ ran the commit
   before the fix.
3. Six cases went to `covered_by_unit_test` on the strength of PR descriptions
   ("per PR #5 description"). The model never saw a line of the test file: test
   patches are stripped from the prompt and repo-wide test discovery found
   nothing. Two of those cases were not covered — the XML test puts
   `<success>` at character 5, not past 256, and the multibyte test asserts
   `responseBytes` but never the excerpt's length.
4. "Covered" assumed CI runs the suite. Nothing does: no workflow files, and
   the image build's `npm test` is `echo … && exit 0`.
5. "A regression test that proves the exploit path" and "an explicit byte
   limit" were never delivered, and the plan never said the work falls short.
6. "The committed node-fetch 2.x resolution" — the Dockerfile copies only
   package.json and runs `npm i`, so the lockfile is not what ships.

Rules A–F in the generator prompt answer each of these. This module is the part
that does not depend on the model following them: it reads the evidence (tests
at the merge commit, CI config, Dockerfiles, lockfiles, every comment with its
id), states the build and CI facts itself, and after generation demotes any
`covered_by_unit_test` claim it cannot prove, puts a deploy check in front of
every case that needs a deployed environment, and turns the requirement check
into an Acceptance gaps section.

Every answer here keeps "could not check" distinct from "no" — a failed read is
never reported as a finding about the repo.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A. Comments
# ---------------------------------------------------------------------------

#: Phrases that, near the top of a comment, mark it as a note on how to test
#: or verify the work. Such a note is a primary source: its environment, setup,
#: trigger, verification and rollback steps belong in the matching cases.
_PRIMARY_NOTE_MARKERS: tuple[tuple[str, str], ...] = (
    ("testing notes", "testing notes"),
    ("test notes", "testing notes"),
    ("qa notes", "QA notes"),
    ("qa note", "QA notes"),
    ("how to verify", "how to verify"),
    ("how to test", "how to verify"),
    ("steps to verify", "how to verify"),
    ("verification steps", "how to verify"),
    ("kickoff research", "kickoff research"),
    ("manual test plan", "test plan"),
    ("test plan", "test plan"),
    ("test cases", "test plan"),
    ("steps to reproduce", "reproduction steps"),
    ("repro steps", "reproduction steps"),
)

#: How far into a comment a marker counts. A heading sits at the top; the same
#: words in the middle of a discussion do not make it a testing note.
_PRIMARY_NOTE_WINDOW = 200


def primary_note_kind(body: str | None) -> str | None:
    """The kind of testing note a comment is, or None for ordinary discussion."""
    head = " ".join((body or "")[:_PRIMARY_NOTE_WINDOW].lower().split())
    for marker, kind in _PRIMARY_NOTE_MARKERS:
        if marker in head:
            return kind
    return None


def primary_notes(comments: list[dict] | None) -> list[dict]:
    """The comments that are testing notes, each as ``{id, author, kind}``.

    A comment without an id cannot be cited, so it is not listed — the prompt
    still shows its text. The bot's own plan comments are never notes.
    """
    from .jira_client import carries_bot_plan_marker

    out = []
    for c in comments or []:
        if carries_bot_plan_marker(c.get("body") or ""):
            continue
        kind = primary_note_kind(c.get("body"))
        if kind and c.get("id"):
            out.append({"id": str(c["id"]), "author": c.get("author") or "Unknown", "kind": kind})
    return out


def note_texts(comments: list[dict] | None) -> dict[str, str]:
    """Testing notes by comment id, for checks that accept a note as a source."""
    ids = {n["id"] for n in primary_notes(comments)}
    return {str(c["id"]): c.get("body") or "" for c in comments or [] if str(c.get("id")) in ids}


# ---------------------------------------------------------------------------
# E. Requirements
# ---------------------------------------------------------------------------

#: Description headings whose content is something the work must do.
_REQUIREMENT_HEADINGS = (
    "what to change", "done when", "definition of done", "acceptance criteria",
    "requirements", "success criteria", "expected behavior", "expected behaviour",
    "expected result", "fix requirements",
)

_BULLET_RE = re.compile(r"^\s*(?:[•\-\*·▪◦]|\d+[.)])\s*")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated paragraphs, each folded onto one line.

    Jira's ADF-to-text puts inline code and bullets on lines of their own, so
    a single sentence can span several lines; folding is what makes a quote
    from it readable.
    """
    out, cur = [], []
    for line in (text or "").splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return [" ".join(p.split()) for p in out]


def _is_heading(par: str) -> bool:
    if _BULLET_RE.match(par) or len(par) > 60:
        return False
    return not par.rstrip().endswith((".", ",", ";", "!", "?"))


def _heading_name(par: str) -> str | None:
    low = par.lower().strip().rstrip(":").strip()
    for h in _REQUIREMENT_HEADINGS:
        if low == h or low.startswith(h + " ") or low.startswith(h + ":"):
            return h
    return None


def extract_requirements(description: str | None, acs: Iterable[str] = ()) -> list[dict]:
    """Every requirement the ticket states, as ``{id, source, text}``.

    Bullets under a requirement heading are one requirement each; prose is
    split into sentences. Acceptance criteria the AC extractor found are
    appended when the headings did not already carry them. Ids (R1, R2, …)
    are stable for one description, which is all the prompt needs.
    """
    reqs: list[dict] = []
    section: str | None = None
    for par in _paragraphs(description or ""):
        if _is_heading(par):
            section = _heading_name(par)
            continue
        if not section:
            continue
        if _BULLET_RE.match(par):
            items = [_BULLET_RE.sub("", par, count=1).strip()]
        else:
            items = [s.strip() for s in _SENTENCE_SPLIT_RE.split(par)]
        for item in items:
            if len(item) >= 8:
                reqs.append({"source": section.capitalize(), "text": item})
    seen = {r["text"].lower() for r in reqs}
    for ac in acs or ():
        text = " ".join(str(ac).split())
        if text and text.lower() not in seen:
            reqs.append({"source": "Acceptance criteria", "text": text})
            seen.add(text.lower())
    for i, r in enumerate(reqs, 1):
        r["id"] = f"R{i}"
    return reqs


_GAP_STATUSES = ("unmet", "partial", "not_verifiable")


def build_acceptance_gaps(requirements: list[dict], checks: list[dict] | None) -> list[dict]:
    """The Acceptance gaps section: every requirement not shown to be met.

    ``checks`` is the model's ``requirement_check``. A requirement the model
    did not assess is a gap too — silence is not evidence that it was met.
    """
    by_id = {}
    for c in checks or []:
        if isinstance(c, dict) and c.get("id"):
            by_id[str(c["id"]).strip()] = c
    gaps = []
    for r in requirements:
        c = by_id.get(r["id"])
        status = (c or {}).get("status")
        if c is None:
            status = "not_assessed"
        elif status not in _GAP_STATUSES:
            continue
        gaps.append({
            "id": r["id"],
            "source": r["source"],
            "requirement": r["text"],
            "status": status,
            "missing": ((c or {}).get("missing") or "").strip()
            or ("the generator did not assess this requirement" if c is None else ""),
            "evidence": ((c or {}).get("evidence") or "").strip() or "no code location given",
        })
    return gaps


# ---------------------------------------------------------------------------
# C/D/F. Repo evidence — selecting files
# ---------------------------------------------------------------------------

_TEST_DIR_SEGMENTS = frozenset({"test", "tests", "__tests__", "spec", "specs", "__specs__"})
_TEST_NAME_RE = re.compile(
    r"(\.(test|spec|unit|e2e|int|integration)\.[cm]?[jt]sx?$)"
    r"|(_test\.(go|py|rb)$)|(^test_.*\.py$)|(_spec\.rb$)"
    r"|((Test|Tests|Spec)\.(cs|java|kt|swift|php)$)"
)
_SOURCE_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".go", ".rb",
                ".cs", ".java", ".kt", ".swift", ".php", ".vue", ".svelte")


def is_test_path(path: str) -> bool:
    parts = (path or "").split("/")
    if not parts[-1].endswith(_SOURCE_EXTS):
        return False
    if any(p.lower() in _TEST_DIR_SEGMENTS for p in parts[:-1]):
        return True
    return bool(_TEST_NAME_RE.search(parts[-1]))


# Every CI provider whose config lives in the repo. SK-2719's plan said
# mobile-api "has no CI configuration" while deployment/codefresh/pipeline.yaml
# ran `npm test` at the merge commit (and an Argo pipeline kept in the
# deployments repo ran it on master, see build_targets.discover_deployments):
# the list knew GitHub Actions, Jenkins and a few hosted CIs and nothing else.
_CI_PATH_RE = re.compile(
    r"^(\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml|(?:.*/)?Jenkinsfile[^/]*|\.circleci/config\.ya?ml"
    r"|azure-pipelines\.ya?ml|bitbucket-pipelines\.yml|buildspec[^/]*\.ya?ml|\.buildkite/[^/]+\.ya?ml"
    r"|\.drone\.ya?ml|cloudbuild\.ya?ml|\.travis\.yml"
    r"|(?:.*/)?codefresh[^/]*\.ya?ml|(?:.*/)?codefresh/[^/]+\.ya?ml"
    r"|(?:.*/)?\.?argo[\w-]*/(?:[^/]+/)*[^/]+\.ya?ml|\.tekton/[^/]+\.ya?ml"
    r"|bitrise\.ya?ml|codemagic\.ya?ml|appveyor\.ya?ml|\.semaphore/[^/]+\.ya?ml|\.woodpecker(?:/[^/]+)?\.ya?ml"
    r"|ci_scripts/[^/]+\.sh|(?:.*/)?fastlane/Fastfile)$"
)
# Fastlane defines lanes; it says nothing about what runs on a commit. A
# Fastfile that runs tests is evidence they CAN run in CI, and one that does
# not is no evidence that nothing else does.
_LANE_PATH_RE = re.compile(r"(?:^|/)fastlane/Fastfile$")


def is_ci_path(path: str) -> bool:
    return bool(_CI_PATH_RE.match(path or ""))


def _ci_provider(path: str, text: str = "") -> str:
    p = (path or "").lower()
    for needle, name in ((".github/workflows", "GitHub Actions"), ("bitbucket-pipelines", "Bitbucket Pipelines"),
                         ("jenkinsfile", "Jenkins"), ("codefresh", "Codefresh"), ("fastfile", "Fastlane"),
                         ("argo", "Argo Workflows"), (".gitlab-ci", "GitLab CI"), (".circleci", "CircleCI"),
                         ("azure-pipelines", "Azure Pipelines"), ("bitrise", "Bitrise"),
                         ("codemagic", "Codemagic"), ("ci_scripts", "Xcode Cloud"), (".tekton", "Tekton")):
        if needle in p:
            return name
    if re.search(r"argo-?pipeline|argoproj|\bWorkflowTemplate\b|workflow\.parameters", text or ""):
        return "Argo Workflows"
    return "CI"


def is_dockerfile(path: str) -> bool:
    base = posixpath.basename(path or "")
    return base == "Dockerfile" or base.startswith("Dockerfile.") or base.endswith(".dockerfile")


_LOCKFILES = {
    "package.json": ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"),
    "pyproject.toml": ("poetry.lock", "uv.lock", "pdm.lock"),
    "Pipfile": ("Pipfile.lock",),
    "Gemfile": ("Gemfile.lock",),
    "go.mod": ("go.sum",),
}


def _stem(path: str) -> str:
    """`request-processor` for `utils/request-processor.js` and its tests."""
    base = posixpath.basename(path).lower().split(".")[0].removeprefix("test_")
    for suf in ("_test", "_spec", "tests", "test", "spec"):
        if base.endswith(suf) and len(base) > len(suf):
            base = base[: -len(suf)]
            break
    return base.strip("._-")


def select_test_files(tree: list[str], changed: list[str], limit: int = 6) -> list[str]:
    """Tests the PRs touched, then tests named after the source they changed."""
    picked = [p for p in changed if is_test_path(p) and p in set(tree)]
    stems = {_stem(p) for p in changed if not is_test_path(p) and p.endswith(_SOURCE_EXTS)}
    stems.discard("")
    for p in tree:
        if len(picked) >= limit:
            break
        if p not in picked and is_test_path(p) and _stem(p) in stems:
            picked.append(p)
    return picked[:limit]


# ---------------------------------------------------------------------------
# D/F. Dockerfiles, CI and manifests — reading what the build does
# ---------------------------------------------------------------------------

def _logical_lines(text: str) -> list[str]:
    out, cur = [], ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not cur and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        out.append((cur + line).strip())
        cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def parse_dockerfile(text: str) -> dict:
    """COPY sources (from the build context) and RUN commands, in order."""
    copies, runs = [], []
    for line in _logical_lines(text):
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word in ("COPY", "ADD"):
            args = rest.strip()
            if "--from=" in args:
                continue  # a multi-stage copy reads a stage, not the repo
            if args.startswith("["):
                try:
                    parts = [str(a) for a in json.loads(args)]
                except ValueError:
                    parts = args.strip("[]").replace('"', "").split(",")
            else:
                parts = [a for a in args.split() if not a.startswith("--")]
            if len(parts) >= 2:
                copies.extend(p.strip() for p in parts[:-1])
        elif word == "RUN":
            runs.append(rest.strip())
    return {"copies": copies, "runs": runs}


def _rel(src: str) -> str:
    """A COPY source without its leading ``./`` (``.github`` stays intact)."""
    src = src.strip()
    while src.startswith("./"):
        src = src[2:]
    return src.rstrip("/")


def _context_dir(dockerfile: str, copies: list[str], tree: set[str]) -> str:
    """The build context: the repo root when COPY sources resolve from there."""
    for src in copies:
        s = _rel(src)
        if s and (s in tree or any(t.startswith(s + "/") for t in tree)):
            return ""
    return posixpath.dirname(dockerfile)


def _copied(path: str, copies: list[str], ctx: str) -> bool:
    """Whether a COPY/ADD with these sources puts ``path`` into the image."""
    for src in copies:
        s = _rel(src)
        full = posixpath.normpath(posixpath.join(ctx, s)) if s not in ("", ".", "*") else ctx
        full = "" if full == "." else full
        if full == "" or path == full or path.startswith(full + "/"):
            return True
    return False


# `test` as a task name: `test`, `test:unit`, but not `test-utils` or `tests/`.
_TEST_TASK = r"\btest(?![\w-])"
# A package manager or monorepo runner reaches the test task through whatever
# sits between them: `pnpm exec turbo run test --filter=…`, `pnpm --filter web
# test`, `yarn workspace web test`, `nx run-many -t test`. SK-2947's plan said
# "no CI configuration runs a test command" because only `pnpm test` and
# `pnpm run test` were recognised, while ci.yml ran `pnpm exec turbo run test`.
_TEST_CMD_RE = re.compile(
    r"\b(?:(?:npm|npx|yarn|pnpm|bun|turbo|nx|lerna|rush)\b[^\n#]*" + _TEST_TASK
    + r"|jest\b|vitest\b|mocha\b|pytest\b|go\s+test\b"
    r"|dotnet\s+test\b|(?:gradlew?|mvn)\b[^\n]*\btest\b|xcodebuild\b[^\n]*\btest\b|make\s+test\b"
    r"|tox\b|cargo\s+test\b|rspec\b|phpunit\b|swift\s+test\b"
    # Fastlane actions: scan / run_tests run XCTest, gradle(task: "test") runs JUnit.
    r"|\b(?:run_tests|scan)\s*\(|\bgradle\s*\(\s*task:\s*[\"'][\w:]*test)"
)
_PKG_TEST_RE = re.compile(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b")
_INSTALL_RE = re.compile(r"\b(?:npm\s+(?:i|install|ci)|yarn(?:\s+install)?|pnpm\s+(?:i|install))\b")
_NOOP_SCRIPT_RE = re.compile(r"no test specified|^\s*(?:true|:|exit 0)?\s*$|&&\s*exit 0\s*$", re.I)


def is_noop_test_script(script: str | None) -> bool:
    """`echo "Error: no test specified" && exit 0` and friends run nothing."""
    s = (script or "").strip()
    if not s:
        return True
    if re.search(r"\b(jest|vitest|mocha|ava|tap|karma|playwright|cypress|node\s+--test)\b", s):
        return False
    return bool(_NOOP_SCRIPT_RE.search(s))


def _package_test_script(pkg_text: str | None) -> str | None:
    try:
        return ((json.loads(pkg_text or "") or {}).get("scripts") or {}).get("test")
    except (ValueError, AttributeError):
        return None


def analyze_build(
    tree: list[str],
    files: dict[str, str],
    changed: list[str],
    test_paths: list[str],
    mentioned: str = "",
    external_ci: list[dict] | None = None,
    searched_elsewhere: list[str] | None = None,
) -> dict:
    """What CI and the image build actually do with the change and its tests.

    ``files`` holds the CI files, Dockerfiles and manifests that were read.
    ``external_ci`` holds pipelines defined outside the repo, each
    ``{"source": "owner/repo/path", "text"}`` (an Argo pipeline in a
    deployments repo); ``searched_elsewhere`` names the repos looked in.
    Returns ``{"suites": {test_path: {"status", "why"}}, "findings": [...],
    "ci_files": [...], "dockerfiles": [...]}``. A suite's status is ``runs``
    (a CI test command or a real image-build test step reaches it),
    ``not_run`` (CI was found and read, and nothing in it does), or
    ``unknown`` (no CI was found, or a file needed to tell could not be read).
    Finding no CI is never "not run": CI can be defined where this check does
    not look.
    """
    tree_set = set(tree)
    ci_files = sorted(p for p in tree if is_ci_path(p))
    ci_unread = [p for p in ci_files if p not in files]
    ci_sources = [(p, files.get(p) or "") for p in ci_files] + [
        (x["source"], x.get("text") or "") for x in external_ci or [] if x.get("source")]
    # A pipeline (something that runs on a commit), as opposed to a Fastfile's
    # lane library. Only a pipeline that was read and runs no tests is "not run".
    pipelines_read = [p for p, _ in ci_sources if not _LANE_PATH_RE.search(p) and p not in ci_unread]
    ci_test_cmds = []
    # Lines that name a test task in a way the command pattern does not
    # recognise (a job called `test`, a wrapper script). They make the answer
    # "could not tell", never "not run": a pattern miss is not a fact about CI.
    ci_test_hints = []
    for p, text in ci_sources:
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            if _TEST_CMD_RE.search(line):
                ci_test_cmds.append({"file": p, "line": line.strip(), "provider": _ci_provider(p, text)})
                break
            if not ci_test_hints and re.search(_TEST_TASK, line):
                ci_test_hints.append({"file": p, "line": line.strip()})

    findings: list[dict] = []
    docker_runners: list[dict] = []  # image builds whose test step really runs
    docker_noops: list[tuple[str, str]] = []
    relevant_docker: list[str] = []
    # The image the ticket talks about first: a shared directory (utils/) can
    # ship in every function's image, and the finding should lead with the one
    # under test.
    def _named_first(df: str) -> tuple[bool, str]:
        d = posixpath.basename(posixpath.dirname(df))
        return (not (d and re.search(rf"\b{re.escape(d)}\b", mentioned)), df)

    lockfile_hits: list[dict] = []
    for df in sorted((p for p in tree if is_dockerfile(p)), key=_named_first):
        text = files.get(df)
        if text is None:
            continue
        parsed = parse_dockerfile(text)
        ctx = _context_dir(df, parsed["copies"], tree_set)
        ships_change = any(_copied(c, parsed["copies"], ctx) for c in changed if not is_test_path(c))
        if not ships_change:
            continue
        relevant_docker.append(df)
        pkg_srcs = [posixpath.normpath(posixpath.join(ctx, _rel(s)))
                    for s in parsed["copies"] if s.endswith("package.json")]
        whole_ctx = any(s.strip() in (".", "./") for s in parsed["copies"])
        if whole_ctx and posixpath.join(ctx, "package.json").lstrip("/") in tree_set:
            pkg_srcs.append(posixpath.join(ctx, "package.json").lstrip("/"))

        # D: does the image build run the tests?
        for run in parsed["runs"]:
            if not _TEST_CMD_RE.search(run):
                continue
            if _PKG_TEST_RE.search(run) and pkg_srcs:
                pkg = pkg_srcs[0]
                if pkg not in files:
                    docker_runners.append({"dockerfile": df, "cmd": run, "ctx": ctx,
                                           "copies": parsed["copies"], "unknown": True})
                    continue
                script = _package_test_script(files.get(pkg))
                if is_noop_test_script(script):
                    docker_noops.append((df,
                        f"{df} runs `{run}`, but the test script in {pkg} is "
                        f"`{script or '(none)'}`, which runs no tests"
                    ))
                    continue
            docker_runners.append({"dockerfile": df, "cmd": run, "ctx": ctx,
                                   "copies": parsed["copies"], "unknown": False})

        # F: does the image install from the committed lockfile?
        installs = [r for r in parsed["runs"] if _INSTALL_RE.search(r)]
        for pkg in pkg_srcs:
            if not installs:
                break
            locks = []
            d = posixpath.dirname(pkg)
            while True:
                for name in _LOCKFILES["package.json"]:
                    cand = posixpath.join(d, name) if d else name
                    if cand in tree_set:
                        locks.append(cand)
                if not d:
                    break
                d = posixpath.dirname(d)
            if not locks:
                continue
            if any(_copied(lock, parsed["copies"], ctx) for lock in locks):
                continue
            deps = ""
            try:
                dep_map = (json.loads(files.get(pkg) or "{}") or {}).get("dependencies") or {}
                order = sorted(dep_map, key=lambda k: k not in mentioned)
                named = [f"{k} {dep_map[k]}" for k in order]
                if named:
                    deps = " (ranges such as " + ", ".join(named[:4]) + ")"
            except ValueError:
                pass
            lockfile_hits.append({"df": df, "pkg": pkg, "locks": locks, "install": installs[0],
                                  "deps": deps, "copies": parsed["copies"]})

    if lockfile_hits:
        first = lockfile_hits[0]
        others = [h["df"] for h in lockfile_hits[1:]]
        also = (f" The same holds for {', '.join(others)}, which also ship the changed code."
                if others else "")
        findings.append({
            "kind": "lockfile_not_shipped",
            "finding": (
                f"The shipped build does not use the committed lockfile: {first['df']} copies "
                f"{first['pkg']} but not {', '.join(first['locks'])}, then runs `{first['install']}`. "
                f"Dependency versions are resolved when the image is built, from the ranges in "
                f"{first['pkg']}{first['deps']}, so what ships can differ from what the lockfile "
                f"pins.{also}"
            ),
            "evidence": f"{first['df']}: COPY {' '.join(first['copies'])}; RUN {first['install']}",
        })

    suites: dict[str, dict] = {}
    for t in test_paths:
        if ci_unread:
            suites[t] = {"status": "unknown",
                         "why": f"CI config could not be read ({', '.join(ci_unread)})"}
            continue
        if ci_test_cmds:
            c = ci_test_cmds[0]
            also = "".join(f"; {o['provider']} {o['file']} runs `{o['line']}`" for o in ci_test_cmds[1:3])
            suites[t] = {"status": "runs",
                         "why": f"{c['provider']} {c['file']} runs `{c['line']}`{also} "
                                f"(check that its scope includes {t})"}
            continue
        hit = next((r for r in docker_runners if _copied(t, r["copies"], r["ctx"])), None)
        if not hit and ci_test_hints:
            h = ci_test_hints[0]
            suites[t] = {"status": "unknown",
                         "why": (f"{h['file']} has `{h['line']}`, which may run tests but is not a "
                                 f"command this check recognises; read {h['file']} before saying CI "
                                 f"does or does not run {t}")}
            continue
        if hit and hit["unknown"]:
            suites[t] = {"status": "unknown",
                         "why": f"{hit['dockerfile']} runs `{hit['cmd']}` but its package.json could not be read"}
        elif hit:
            suites[t] = {"status": "runs", "why": f"{hit['dockerfile']} runs `{hit['cmd']}` during the image build"}
        elif not pipelines_read:
            where = " and in " + ", ".join(searched_elsewhere) if searched_elsewhere else ""
            lanes = [p for p in ci_files if _LANE_PATH_RE.search(p)]
            why = (f"no CI pipeline was found in the repo (GitHub Actions, Argo, Jenkins, Bitbucket, "
                   f"Codefresh, GitLab, CircleCI and others were looked for){where}"
                   + (f"; {', '.join(lanes)} defines Fastlane lanes, none of which runs tests"
                      if lanes else "")
                   + (f"; {docker_noops[0][1]}" if docker_noops else "")
                   + f". CI may be defined where this check does not look, so whether it runs {t} is unknown")
            suites[t] = {"status": "unknown", "why": why}
        else:
            reasons = [f"none of the CI pipelines ({', '.join(pipelines_read)}) runs a test command"]
            if docker_noops:
                reasons.append(docker_noops[0][1])
                if len(docker_noops) > 1:
                    reasons.append("the same no-op test step is in "
                                   + ", ".join(df for df, _ in docker_noops[1:]))
            not_copied = [r["dockerfile"] for r in docker_runners if not _copied(t, r["copies"], r["ctx"])]
            if not_copied:
                reasons.append(f"{', '.join(not_copied)} runs tests but does not copy {t} into the build")
            suites[t] = {"status": "not_run", "why": "; ".join(reasons)}

    not_run = [t for t, s in suites.items() if s["status"] == "not_run"]
    if not_run:
        findings.insert(0, {
            "kind": "tests_not_run_in_ci",
            "finding": (
                f"Tests exist but are not run in CI: {', '.join(not_run)}. "
                f"{suites[not_run[0]]['why'][:1].upper()}{suites[not_run[0]]['why'][1:]}. "
                "A passing assertion in these files is not proof that the shipped build "
                "behaves that way until someone runs them."
            ),
            "evidence": "; ".join(pipelines_read),
        })
    unknown = [t for t, s in suites.items() if s["status"] == "unknown"]
    if unknown:
        findings.append({
            "kind": "ci_unknown",
            "finding": f"Could not tell whether CI runs {', '.join(unknown)}: {suites[unknown[0]]['why']}.",
            "evidence": "",
        })
    # Say where the tests DO run, so a plan never has to guess it: SK-2719's
    # plan argued from "no CI" that the unit tests proved nothing.
    if ci_test_cmds and any(s["status"] == "runs" for s in suites.values()):
        findings.append({
            "kind": "ci_runs_tests",
            "finding": "CI runs the test suite: " + "; ".join(
                f"{c['provider']} {c['file']} runs `{c['line']}`" for c in ci_test_cmds[:3]) + ".",
            "evidence": "; ".join(c["file"] for c in ci_test_cmds[:3]),
        })
    return {"suites": suites, "findings": findings, "ci_files": ci_files,
            "external_ci": [x["source"] for x in external_ci or [] if x.get("source")],
            "dockerfiles": relevant_docker}


# ---------------------------------------------------------------------------
# C. Proving unit-test coverage
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^\s*`?(?P<path>[^\s`›]+?)(?::\d+(?:-\d+)?)?`?\s*›\s*(?P<name>.+?)\s*$")
_HEARSAY_RE = re.compile(
    r"\b(?:per|from|according to|as stated in|see)\b[^›]*\b(?:PR|pull request|description|commit|summary)\b"
    r"|\bPR\s*#?\d+\s+(?:description|summary|body)\b",
    re.I,
)


def parse_unit_test_ref(ref: str | None) -> tuple[str, str] | None:
    """``(file, test name)`` from ``file › exact test name``, else None."""
    m = _REF_RE.match(ref or "")
    if not m:
        return None
    name = m.group("name").strip().strip("'\"`")
    return (m.group("path"), name) if name else None


def _norm(s: str) -> str:
    return " ".join((s or "").split())


_EACH_RE = re.compile(r"\b(?:it|test)(?:\.(?:only|skip|concurrent))*\.each\b")


def _each_table_start(content: str, i: int) -> int:
    """Where a parameterised test starts, when the name at ``i`` belongs to one.

    In ``it.each([...])("name %s", fn)`` the inputs come before the name, so a
    body taken from the name onward hides which values the test runs. SK-2947's
    coverage check called a correct claim "partial" because the table holding
    "Loading CSS chunk 12 failed." was cut off.
    """
    window = content[max(0, i - 4000):i]
    matches = list(_EACH_RE.finditer(window))
    if not matches:
        return i
    m = matches[-1]
    between = window[m.end():]
    if re.search(r"\b(?:it|test)\s*\(", between) or not between.rstrip().endswith("("):
        return i
    return i - len(window) + m.start()


def extract_test_body(content: str, name: str) -> str | None:
    """The source of the test called ``name`` in ``content``.

    Finds the name as a string literal (JS/TS ``it("…")``, Kotlin backticks)
    or as a function name (Python ``def name``, Go ``func Name``, C#/Java
    methods), then takes the balanced block that follows — braces, or for
    Python the indented block. None when the test is not in the file.
    """
    if not content or not name:
        return None
    for q in ('"', "'", "`"):
        i = content.find(f"{q}{name}{q}")
        if i >= 0:
            i = _each_table_start(content, i)
            break
    else:
        m = re.search(rf"\b(?:def|func|fun|void|Task|async\s+Task)\s+{re.escape(name)}\s*\(", content)
        if not m:
            return None
        i = m.start()
        if content[i:].startswith("def "):
            line_start = content.rfind("\n", 0, i) + 1
            indent = i - line_start
            lines = content[line_start:].splitlines()
            body = [lines[0]]
            for ln in lines[1:]:
                if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
                    break
                body.append(ln)
            return "\n".join(body)
    j = content.find("{", i)
    if j < 0:
        return content[i:i + 4000]
    depth = 0
    for k in range(j, len(content)):
        ch = content[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[i:k + 1]
    return content[i:]


def _evidence_in_body(evidence: str, body: str) -> bool:
    """Every quoted line of the evidence appears in the test body."""
    lines = [_norm(l) for l in (evidence or "").splitlines() if _norm(l)]
    if not lines:
        return False
    flat = _norm(body)
    return all(l in flat for l in lines)


def _find_source(path: str, sources: dict[str, str]) -> str | None:
    if path in sources:
        return path
    hits = [p for p in sources if p.endswith("/" + _rel(path)) or path.endswith("/" + p)]
    return hits[0] if len(hits) == 1 else None


CASE_SECTIONS = ("happy_path", "edge_cases", "integration_tests", "security_negative_tests")


def iter_cases(test_plan) -> Iterable[tuple[str, int, dict]]:
    for section in CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


def _demote(case: dict, reason: str) -> None:
    case["covered_by_unit_test"] = False
    prior = (case.get("unit_test_gap") or "").strip()
    case["unit_test_gap"] = f"{prior} {reason}".strip() if prior and reason not in prior else (prior or reason)


def enforce_unit_test_coverage(
    test_plan,
    test_sources: dict[str, str],
    suites: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Demote every ``covered_by_unit_test`` claim the evidence does not prove.

    A claim stands only when its ``unit_test_ref`` is ``file › exact test
    name``, the file is one we read, the test is in it, the quoted
    ``coverage_evidence`` is inside that test, and CI (or the image build)
    actually runs the file. Returns ``(demoted, surviving)``; each surviving
    entry carries the extracted test body for the semantic check.
    """
    demoted, surviving = [], []
    for section, i, case in iter_cases(test_plan):
        if not case.get("covered_by_unit_test"):
            continue
        ref = case.get("unit_test_ref") or ""
        why = None
        parsed = parse_unit_test_ref(ref)
        body = None
        path = None
        if _HEARSAY_RE.search(ref):
            why = ("coverage was taken from a PR description or commit message, not from the "
                   "test code; not counted as covered")
        elif not parsed:
            why = "no `file › test name` citation, so the coverage cannot be checked; not counted as covered"
        else:
            path = _find_source(parsed[0], test_sources)
            if path is None:
                why = f"{parsed[0]} was not read when this plan was written, so its coverage is unproven"
            else:
                body = extract_test_body(test_sources[path], parsed[1])
                if body is None:
                    why = f"no test named \"{parsed[1]}\" in {path}"
                elif not _evidence_in_body(case.get("coverage_evidence") or "", body):
                    why = (f"the assertion quoted as evidence is not in {path} › {parsed[1]}, so the "
                           "test body was not shown to check this case")
                else:
                    st = (suites or {}).get(path) or {"status": "unknown", "why": "CI was not checked"}
                    if st["status"] == "not_run":
                        why = f"tests exist but are not run in CI ({st['why']}); not treated as proof"
                    elif st["status"] != "runs":
                        why = f"could not confirm CI runs {path} ({st['why']}); not treated as proof"
        if why:
            _demote(case, why)
            demoted.append({"case_id": f"{section}:{i}", "title": case.get("title"), "reason": why})
        else:
            surviving.append({"case_id": f"{section}:{i}", "section": section, "index": i,
                              "case": case, "test_ref": f"{path} › {parsed[1]}", "test_body": body})
    return demoted, surviving


def apply_coverage_verdicts(surviving: list[dict], verdicts: dict[str, dict] | None) -> list[dict]:
    """Demote the cases the semantic check did not call fully covered.

    ``verdicts`` maps case_id to ``{"verdict": "full"|"partial"|"none",
    "missing": str}``. None means the check could not run: then nothing is
    confirmed, and every surviving claim is demoted with that reason.
    """
    demoted = []
    for s in surviving:
        v = (verdicts or {}).get(s["case_id"]) if verdicts is not None else None
        if verdicts is None:
            why = "the coverage check could not run, so this case's coverage is unconfirmed"
        elif not v:
            why = "the coverage check returned no verdict for this case; not counted as covered"
        elif v.get("verdict") == "full":
            continue
        elif v.get("verdict") == "partial":
            why = f"{s['test_ref']} covers only part of this case — missing: {v.get('missing') or 'unspecified'}"
        else:
            why = f"{s['test_ref']} does not check this case's condition: {v.get('missing') or 'unspecified'}"
        _demote(s["case"], why)
        demoted.append({"case_id": s["case_id"], "title": s["case"].get("title"), "reason": why})
    return demoted


COVERAGE_CRITIC_SYSTEM_PROMPT = """You check whether an automated test proves a manual test case.

For each item you get one manual case (title, steps, expected result) and the
exact source of one test. Answer "full" only if an assertion in that test body
checks the case's exact condition: the same input shape, the same position or
boundary, and EVERY claim in the expected result. Anything less is "partial"
(some claims asserted, others not) or "none".

Be literal. Examples of "partial" or "none":
- The case puts a value past a size or position limit; the test puts it first.
- The case claims two things (a value AND its unit or format); the test asserts one.
- The test checks a mock was called, the case checks an observable outcome.
- The test name says it, but no assertion in the body checks it.

For anything short of "full", `missing` names exactly what the test does not
assert, in one sentence."""

REPORT_COVERAGE_TOOL = {
    "name": "report_coverage",
    "description": "Report, per case, whether the cited test proves it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["full", "partial", "none"]},
                        "missing": {"type": "string"},
                    },
                    "required": ["case_id", "verdict"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def build_coverage_critic_message(surviving: list[dict]) -> str:
    parts = []
    for s in surviving:
        c = s["case"]
        steps = "\n".join(f"  {n}. {st}" for n, st in enumerate(c.get("steps") or [], 1))
        parts.append(
            f"### {s['case_id']}\nTitle: {c.get('title')}\nSteps:\n{steps}\n"
            f"Expected: {c.get('expected')}\nTest data: {c.get('test_data') or '-'}\n"
            f"Cited test: {s['test_ref']}\n```\n{s['test_body'][:6000]}\n```"
        )
    return "\n\n".join(parts)


def parse_coverage_verdicts(raw) -> dict[str, dict]:
    out = {}
    for v in (raw or {}).get("verdicts") or []:
        if isinstance(v, dict) and v.get("case_id"):
            out[str(v["case_id"])] = {"verdict": v.get("verdict"), "missing": v.get("missing") or ""}
    return out


# ---------------------------------------------------------------------------
# B. Deployment
# ---------------------------------------------------------------------------

#: Environments a case can only run in once the change is deployed.
_ENV_NAME_RE = re.compile(
    r"\b(integ|integration environment|staging|stage|prod|production|uat|qa environment|"
    r"sandbox|non-?prod|dev environment|preview environment|cluster|pod)\b",
    re.I,
)
#: A case's own `environment` field may just say "deployed"; a checklist line
#: that mentions "the deployed commit" while running tests locally may not.
_DEPLOYED_ENV_RE = re.compile(_ENV_NAME_RE.pattern[:-3] + r"|deployed)\b", re.I)
_LOCAL_ENV_RE = re.compile(
    r"\b(local|offline|harness|localhost|developer machine|unit|jest|ci only|in-process|"
    r"no(?:t)? deploy\w*|source inspection|code inspection|static analysis|code review)\b",
    re.I,
)
_DEPLOY_CHECK_RE = re.compile(
    r"(deploy|image tag|argo ?cd|running build|build number|app version|version endpoint|pod image|"
    r"release tag|commit)",
    re.I,
)


def needs_deployed_env(case: dict) -> bool:
    env = f"{case.get('environment') or ''} {case.get('preconditions') or ''}"
    if _LOCAL_ENV_RE.search(case.get("environment") or ""):
        return False
    return bool(_DEPLOYED_ENV_RE.search(env)) or bool(
        re.search(r"https?://(?!localhost|127\.0\.0\.1)", case.get("environment") or "")
    )


#: A first step that IS a deploy check (and nothing else), so the code's check
#: may take its place. Looser matches (a step that mentions a commit) are left
#: alone: replacing a real step would drop part of the case.
_DEPLOY_STEP_RE = re.compile(
    r"^\s*(?:deploy(?:ment)?\s+check|build\s+check|verify\s+the\s+deploy|confirm\s+the\s+deploy|"
    r"check\s+the\s+deploy)|\b(?:image\s+tag|argo\s?cd\s+app|pod'?s?\s+image|values\.ya?ml\b[^.]*\btag)",
    re.I,
)


def is_model_deploy_step(step: str) -> bool:
    return isinstance(step, str) and bool(_DEPLOY_STEP_RE.search(step)) and bool(
        re.search(r"\b(confirm|check|verify|make sure|ensure)\b", step, re.I))


def has_deploy_check_first(case: dict) -> bool:
    steps = case.get("steps") or []
    first = steps[0] if steps and isinstance(steps[0], str) else ""
    return bool(_DEPLOY_CHECK_RE.search(first)) and bool(
        re.search(r"\b(confirm|check|verify|make sure|ensure)\b", first, re.I)
    )


def deploy_check_step(commit: str | None, how: str | None) -> str:
    sha = (commit or "")[:7]
    what = f"merged commit {sha}" if sha else "the merged change"
    how = how or ("read the deployment repo's image tag for this environment, the ArgoCD "
                  "app's image, or the running pod's image")
    return (f"Deploy check: confirm this environment runs {what} (or a later build that "
            f"contains it) — {how}. If it does not, stop: the case is blocked on deployment.")


def enforce_deploy_precondition(test_plan, commit: str | None = None, how: str | None = None,
                                extra_cases: list[dict] | None = None, *,
                                targets: list[dict] | None = None,
                                when: str | None = None) -> list[str]:
    """Put a build check first in every case that needs a running build.

    With ``targets`` (``build_targets.build_targets``) the check is chosen per
    case: the repos the case exercises, each with its OWN merge commit, each
    checked the way its platform ships — a deployed image that contains the
    merge (an ancestor check, not an exact tag) for a service, the installed
    app's version and source commit for an iOS or Android build. Without
    targets it is the single-commit check older callers relied on.

    The check is the code's, not the model's. A first step the model wrote as
    its own deploy check is replaced rather than kept: on SK-2719 that step
    demanded one exact image tag, and on SK-2889 an iOS case's precondition
    checked a build number while the step code added asked for an ArgoCD app.
    Replacing in place keeps the step count, so nothing is renumbered. An
    exact image tag a case pins in its preconditions, environment or steps is
    rewritten to "a build that contains <merge commit>".

    Runs before step grounding, so the warnings that pass writes ("step 3
    names …") count the added step.
    """
    from .build_targets import build_check_step, short_requirement, targets_for_case, unpin_tags

    fixed = []
    cases = list(iter_cases(test_plan)) + [("needs_spec_cases", i, c)
                                            for i, c in enumerate(extra_cases or [])]
    for section, i, case in cases:
        if not isinstance(case, dict):
            continue
        mine = targets_for_case(case, targets) if targets else []
        if mine:
            for key in ("preconditions", "environment"):
                if isinstance(case.get(key), str):
                    case[key], n = unpin_tags(case[key], mine)
                    if n:
                        case["pinned_tag_replaced"] = True
        if not needs_deployed_env(case):
            continue
        step = build_check_step(mine, when) if mine else deploy_check_step(commit, how)
        steps = list(case.get("steps") or [])
        if has_deploy_check_first(case):
            if not targets or steps[0] == step or not is_model_deploy_step(steps[0]):
                continue
            # The model's own check: replaced in place, so step numbers hold.
            case["steps"] = [step] + [unpin_tags(s, mine)[0] if isinstance(s, str) else s
                                      for s in steps[1:]]
            case["deploy_check_replaced"] = True
            fixed.append(f"{section}:{i}")
            continue
        case["steps"] = [step] + [unpin_tags(s, mine)[0] if (mine and isinstance(s, str)) else s
                                  for s in steps]
        case["deploy_check_added"] = True
        # step_grounding numbers steps from 1 (0 is the preconditions); the
        # new first step moves every numbered one down by one.
        for ref in case.get("step_grounding") or []:
            if isinstance(ref, dict) and isinstance(ref.get("step"), int) and ref["step"] > 0:
                ref["step"] += 1
        fixed.append(f"{section}:{i}")
    # Checklist lines are bare strings and have skipped every case guard
    # before (SK-2342). One that sends the tester to a deployed environment
    # gets the same precondition, as a prefix — the line count, which the
    # progress key fingerprints, is unchanged.
    lines = getattr(test_plan, "regression_checklist", None) or []
    sha = (commit or "")[:7]
    for i, line in enumerate(lines):
        if not isinstance(line, str) or not _ENV_NAME_RE.search(line):
            continue
        if "Deploy check first" in line or "Build check first" in line:
            continue
        if targets:
            mine = targets_for_case({"expected": line}, targets)
            if not mine or all(t["commit"] and t["commit"][:7] in line for t in mine):
                continue
            prefix = f"Build check first: confirm {short_requirement(mine)}. "
        else:
            if sha and sha in line:
                continue
            what = f"merged commit {sha}" if sha else "the merged change"
            prefix = f"Deploy check first: confirm the environment runs {what} or later. "
        m = re.match(r"^(\s*(?:🔴|🟡|🟢)\s*)", line)
        head = m.group(1) if m else ""
        lines[i] = head + prefix + line[len(head):]
        fixed.append(f"regression_checklist:{i}")
    return fixed


def build_deployment_status(deploy_state: list[dict] | None, model_status: dict | None,
                            repos: list[str], when: str, targets: list[dict] | None = None) -> dict:
    """The banner at the top of the plan: is the change running anywhere?

    With ``targets``, an app repo (iOS, Android) is never said to lack a
    deployment source: an app has no deployment, and its cases check the
    installed build instead. A deployments repo is not a target at all.
    """
    warnings, deployed_envs = [], []
    if targets is not None:
        from .build_targets import short_requirement

        for t in targets:
            if t["platform"] in ("ios", "android"):
                warnings.append(
                    f"{t['repo']} is an {'iOS' if t['platform'] == 'ios' else 'Android'} app, so there "
                    f"is no deployment to read: every case on it starts by checking that "
                    f"{short_requirement([t])}."
                )
        repos = [t["repo"] for t in targets if t["platform"] not in ("ios", "android")]
    for e in deploy_state or []:
        if e.get("error"):
            warnings.append(f"Could not check {e['env']} ({e['repo']}): {e['error']}.")
            continue
        prs = e.get("prs") or []
        missing = [f"#{p['number']}" for p in prs if p.get("included") is False]
        if prs and len(missing) == len(prs):
            warnings.append(
                f"Not deployed to {e['env']} as of {e.get('source') or 'the deployment config'} "
                f"({when}): it runs {e.get('tag')}, which does not contain {', '.join(missing)}."
            )
        elif missing:
            warnings.append(f"Partly deployed to {e['env']}: {e.get('tag')} lacks {', '.join(missing)}.")
        elif prs and all(p.get("included") for p in prs):
            deployed_envs.append(e["env"])
    ms = model_status if isinstance(model_status, dict) else {}
    if (ms.get("deployed") in ("no", "unknown")) and (ms.get("statement") or "").strip():
        src = f" ({ms['source']})" if ms.get("source") else ""
        warnings.append(f"{ms['statement'].strip()}{src}")
    checked = {e.get("repo") for e in deploy_state or []}
    unchecked = [r for r in repos if r not in checked]
    if unchecked and targets is not None:
        warnings.append(
            f"Deployment was not checked: no deployment source is configured or was found for "
            f"{', '.join(unchecked)}. Every case that needs a deployed environment "
            "starts with a deploy check."
        )
    elif targets is None and not deploy_state and not warnings:
        warnings.append(
            f"Deployment was not checked: no deployment source is configured for "
            f"{', '.join(repos) or 'this repo'}. Every case that needs a deployed environment "
            "starts with a deploy check."
        )
    if deployed_envs and not [w for w in warnings if not w.startswith(tuple(
            t["repo"] for t in targets or [] if t["platform"] in ("ios", "android")))]:
        status = "deployed"
    elif any(w.startswith("Not deployed") for w in warnings) or ms.get("deployed") == "no":
        status = "not_deployed"
    else:
        status = "unknown"
    return {"status": status, "deployed_envs": deployed_envs, "warnings": warnings}


# ---------------------------------------------------------------------------
# A. Did the plan use the notes?
# ---------------------------------------------------------------------------

def check_note_usage(test_plan, notes: list[dict], disagreements: list[dict] | None,
                     comment_ids: Iterable[str] = (),
                     extra_cases: list[dict] | None = None) -> dict:
    """Which testing notes each case cites, and which notes nothing used.

    Unknown comment ids are dropped from cases — a citation of a comment that
    does not exist is worse than none.
    """
    known = {n["id"] for n in notes}
    used: dict[str, list[str]] = {n["id"]: [] for n in notes}
    targets = list(iter_cases(test_plan)) + [("needs_spec_cases", i, c)
                                              for i, c in enumerate(extra_cases or [])]
    real = {str(c) for c in comment_ids} | known
    for section, i, case in targets:
        if not case.get("source_comment_ids"):
            continue
        ids = [str(x).strip() for x in case["source_comment_ids"] if str(x).strip() in real]
        case["source_comment_ids"] = ids
        for cid in ids:
            if cid in used:
                used[cid].append(f"{section}:{i}")
    dis = [d for d in (disagreements or []) if isinstance(d, dict) and str(d.get("comment_id") or "") in known]
    disputed = {str(d["comment_id"]) for d in dis}
    unused = [n for n in notes if not used[n["id"]] and n["id"] not in disputed]
    return {
        "primary_notes": [{**n, "used_by": used[n["id"]]} for n in notes],
        "disagreements": dis,
        "unused": unused,
    }


# ---------------------------------------------------------------------------
# Gathering — the only part that talks to GitHub
# ---------------------------------------------------------------------------

#: Whole test files, not the 4,000-character head the repository context
#: keeps: the assertion that proves or disproves a case can be anywhere.
_TEST_FILE_MAX_CHARS = 40_000


def _ref_for(prs: list[dict]) -> str | None:
    merged = [p for p in prs if (p.get("status") or "").upper() == "MERGED" and p.get("merge_commit_sha")]
    if merged:
        return max(merged, key=lambda p: p.get("merged_at") or "")["merge_commit_sha"]
    open_ = [p for p in prs if p.get("head_sha")]
    return open_[0]["head_sha"] if open_ else None


async def gather_repo_evidence(github, pull_requests: list[dict] | None, mentioned: str = "",
                               deployments: dict | None = None) -> list[dict]:
    """Read tests, CI config, Dockerfiles and manifests at each repo's merge commit.

    One entry per code repo the ticket's PRs touch::

        {"repo", "ref", "error", "test_sources": {path: text},
         "truncated": [paths], "build": analyze_build(...),
         "platform": "backend" | "ios" | "android" | "web" | "unknown",
         "app_version": build_targets.parse_app_version(...) or None}

    ``ref`` is that repo's OWN merge commit, never another repo's.
    ``deployments`` (``build_targets.discover_deployments``) adds the CI
    pipelines kept outside the repo and names the deployments repos, which
    are skipped: nothing a tester runs is built from them. ``error`` is set,
    and nothing else is claimed about the repo, when its tree could not be
    listed.
    """
    from .build_targets import detect_platform, parse_app_version, version_files

    deployments = deployments or {}
    skip = set(deployments.get("deployments_repos") or [])
    by_repo: dict[str, list[dict]] = {}
    for pr in pull_requests or []:
        if isinstance(pr, dict) and pr.get("repository") and pr["repository"] not in skip:
            by_repo.setdefault(pr["repository"], []).append(pr)
    out = []
    for repo, prs in by_repo.items():
        ref = _ref_for(prs)
        entry = {"repo": repo, "ref": ref, "error": None, "test_sources": {},
                 "truncated": [], "build": None, "platform": None, "app_version": None}
        out.append(entry)
        tree = await github.list_tree(repo, ref) if (github and ref) else None
        if tree is None:
            entry["error"] = (f"could not list {repo} at {(ref or 'its merge commit')[:7]}, so its "
                              "tests, CI and build were not checked")
            continue
        changed = sorted({f.get("filename") for pr in prs for f in (pr.get("files_changed") or [])
                          if isinstance(f, dict) and f.get("filename") and f.get("status") != "removed"})
        tests = select_test_files(tree, changed)
        dockerfiles = [p for p in tree if is_dockerfile(p)][:20]
        ci = [p for p in tree if is_ci_path(p)][:12]
        entry["platform"] = detect_platform(tree, changed)
        versions = version_files(tree, entry["platform"])
        first = await github.fetch_files_at_ref(repo, ref, tests + dockerfiles + ci)
        # An app's project.pbxproj is routinely past the 300 KB cap that keeps
        # generated blobs out of prompts; read below the cap, it fell back to
        # project.yml's placeholder 1.0.0 where Xcode builds 2.13.0.
        version_texts = await github.fetch_files_at_ref(repo, ref, versions, max_bytes=8_000_000) \
            if versions else {}
        entry["app_version"] = parse_app_version(
            entry["platform"], {p: version_texts[p] for p in versions if p in version_texts})
        # Manifests the Dockerfiles copy, plus the root one.
        manifests = {"package.json"} if "package.json" in tree else set()
        for df in dockerfiles:
            parsed = parse_dockerfile(first.get(df) or "")
            ctx = _context_dir(df, parsed["copies"], set(tree))
            for s in parsed["copies"]:
                if s.endswith("package.json"):
                    manifests.add(posixpath.normpath(posixpath.join(ctx, _rel(s))))
        second = await github.fetch_files_at_ref(repo, ref, sorted(m for m in manifests if m in tree))
        files = {**first, **second}
        for t in tests:
            if t in files:
                text = files[t]
                if len(text) > _TEST_FILE_MAX_CHARS:
                    text = text[:_TEST_FILE_MAX_CHARS]
                    entry["truncated"].append(t)
                entry["test_sources"][t] = text
        entry["build"] = analyze_build(
            tree, files, changed, list(entry["test_sources"]), mentioned,
            external_ci=(deployments.get("pipelines") or {}).get(repo),
            searched_elsewhere=sorted(skip),
        )
        unread_tests = [t for t in tests if t not in files]
        if unread_tests:
            entry["build"]["findings"].append({
                "kind": "tests_unread",
                "finding": f"Could not read {', '.join(unread_tests)}, so no case may rely on them.",
                "evidence": "",
            })
    return out


def render_repo_evidence(entries: list[dict]) -> str:
    """The prompt section: test sources in full, then the build and CI facts."""
    if not entries:
        return ""
    lines = []
    for e in entries:
        lines.append(f"### {e['repo']} at {(e['ref'] or '?')[:7]}")
        if e["error"]:
            lines.append(f"COULD NOT CHECK: {e['error']}. Do not call any case covered by a "
                         "unit test, and make no claim about CI or what ships.")
            continue
        if e.get("platform") and e["platform"] != "unknown":
            app = e.get("app_version") or {}
            ver = (f"; an app build at this commit reports version {app['version']}"
                   + (f" ({app['build']})" if app.get("build") else "") + f", from {app['file']}"
                   if app.get("version") else "")
            lines.append(f"- BUILD/CI FACT: ships as a {e['platform']} "
                         f"{'app build' if e['platform'] in ('ios', 'android') else 'deployment'}; its merge "
                         f"commit is {(e['ref'] or '?')[:7]}{ver}.")
        for f in (e["build"] or {}).get("findings") or []:
            lines.append(f"- BUILD/CI FACT: {f['finding']}")
        suites = (e["build"] or {}).get("suites") or {}
        for path, text in e["test_sources"].items():
            st = suites.get(path, {"status": "unknown", "why": "not checked"})
            cut = " (TRUNCATED — tests past this point were not read)" if path in e["truncated"] else ""
            lines.append(f"\n--- TEST FILE {path} — CI status: {st['status'].upper()} ({st['why']}){cut} ---")
            lines.append(text)
        if not e["test_sources"]:
            lines.append("- No test file for the changed code was found at this commit.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering for the CLI and the MCP server (the UI and Jira render in
# frontend/src/utils/markdown.js and TestPlanDisplay.jsx)
# ---------------------------------------------------------------------------

_GAP_LABEL = {"unmet": "Not met", "partial": "Partly met",
              "not_verifiable": "Cannot be confirmed from the code", "not_assessed": "Not assessed"}


def render_evidence_lines(plan: dict, markdown: bool = True) -> list[str]:
    """The top-of-plan block: deployment, Acceptance gaps, Build & CI, notes."""
    out: list[str] = []

    def head(md: str, text: str):
        out.extend([md if markdown else text, ""] if markdown else ["", text, "-" * 60, ""])

    ds = plan.get("deployment_status") or {}
    if ds.get("warnings"):
        head("## 🚫 Not Deployed" if ds.get("status") == "not_deployed" else "## 🚢 Deployment",
             "NOT DEPLOYED" if ds.get("status") == "not_deployed" else "DEPLOYMENT")
        out += [f"{'> ' if markdown else ''}⚠️ {w}" for w in ds["warnings"]] + [""]
    gaps = plan.get("acceptance_gaps") or []
    if gaps:
        head(f"## ❗ Acceptance Gaps ({len(gaps)})", f"ACCEPTANCE GAPS ({len(gaps)})")
        for g in gaps:
            out.append(f"- {_GAP_LABEL.get(g['status'], g['status'])} — {g['source']}: “{g['requirement']}”")
            if g.get("missing"):
                out.append(f"  - Missing: {g['missing']}")
            if g.get("evidence"):
                out.append(f"  - Code: {g['evidence']}")
        out.append("")
    facts = plan.get("build_and_ci") or []
    if facts:
        head("## 🏗️ Build & CI", "BUILD & CI")
        out += [f"- {f['finding']}" for f in facts] + [""]
    notes = plan.get("evidence_notes") or {}
    if notes.get("unused") or notes.get("disagreements"):
        head("## 📝 Testing Notes on the Ticket", "TESTING NOTES ON THE TICKET")
        out += [f"- ⚠️ Comment {n['id']} ({n['kind']}) was not used by any case." for n in notes.get("unused") or []]
        out += [f"- Plan departs from comment {d['comment_id']}: the note says {d['note_says']}; "
                f"the plan {d['plan_does']} — {d['why']}" for d in notes.get("disagreements") or []]
        out.append("")
    return out
