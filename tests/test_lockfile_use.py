"""Rule F: whether the image installs from the lockfile, read from every stage.

SK-3060's plan 721 said "The shipped build does not use the committed
lockfile: apps/server/Dockerfile copies apps/server/package.json but not
pnpm-lock.yaml, then runs `npm install -g pnpm@10.34.5`." The image does use it:
`turbo prune --docker` writes a pruned lockfile, the installer stage copies it
with `COPY --from=builder` and runs `pnpm install --frozen-lockfile`. The check
skipped every `COPY --from`, guessed the build context from the Dockerfile's
directory, and read a global CLI install as the dependency install.

Fixtures: tests/fixtures/lockfile_multistage (placeholders for registry hosts
and image names; line numbers match the original Dockerfile).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.app import evidence_checks as ev
from src.app.models import TestPlan

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "lockfile_multistage"
TURBO = (FIXTURES / "turbo_prune.Dockerfile").read_text()
NO_LOCK = (FIXTURES / "no_lockfile.Dockerfile").read_text()

REPO = "example/agent-app"
PR = {"repository": REPO, "status": "MERGED", "merge_commit_sha": "374a83d" + "0" * 33,
      "merged_at": "2026-11-10T00:00:00Z",
      "files_changed": [{"filename": "pnpm-lock.yaml", "status": "modified"},
                        {"filename": "apps/server/package.json", "status": "modified"},
                        {"filename": "apps/server/src/index.ts", "status": "modified"}]}
SERVER_PKG = json.dumps({"name": "@example/server",
                         "dependencies": {"fastify": "^5.6.0", "zod": "^4.1.0"}})


def _repo(dockerfile: str) -> tuple[list[str], dict[str, str]]:
    tree = ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "turbo.json",
            "apps/server/Dockerfile", "apps/server/package.json", "apps/server/src/index.ts",
            "apps/web/package.json", "apps/web/src/page.tsx"]
    files = {"apps/server/Dockerfile": dockerfile, "apps/server/package.json": SERVER_PKG,
             "package.json": json.dumps({"name": "root", "private": True})}
    return tree, files


class FakeGitHub:
    def __init__(self, tree, files):
        self.tree, self.files = tree, files

    async def list_tree(self, repo, ref):
        return list(self.tree)

    async def fetch_files_at_ref(self, repo, ref, paths, max_bytes=300_000):
        return {p: self.files[p] for p in paths if p in self.files}


async def _evidence(dockerfile: str, extra_files: dict | None = None, extra_tree=()):
    tree, files = _repo(dockerfile)
    files.update(extra_files or {})
    [entry] = await ev.gather_repo_evidence(FakeGitHub(tree + list(extra_tree), files), [PR], "zod fastify")
    return entry


def _lock_findings(entry) -> list[dict]:
    return [f for f in entry["build"]["findings"] if f["kind"].startswith("lockfile_")]


# ── the SK-3060 shape: turbo prune + pruned lockfile + frozen install ────────

@pytest.mark.asyncio
async def test_turbo_prune_build_installs_from_the_pruned_lockfile():
    entry = await _evidence(TURBO)
    [f] = _lock_findings(entry)
    assert f["kind"] == "lockfile_shipped", f["finding"]
    text = f["finding"]
    assert text.startswith("The shipped build installs from the committed lockfile")
    assert "does not use" not in text
    assert "pruned lockfile that `turbo prune` writes (line 24)" in text
    assert "apps/server/Dockerfile line 30" in text
    assert "COPY --from=builder /app/out/pnpm-lock.yaml ./pnpm-lock.yaml" in text
    assert "apps/server/Dockerfile:31 runs `pnpm install --frozen-lockfile`" in text
    # The global CLI install is named for what it is, never as the install.
    assert "`npm install -g pnpm@10.34.5` at apps/server/Dockerfile:6 installs the pnpm@10.34.5 CLI only" in text
    assert [(c["path"], c["line"]) for c in f["cites"]] == [
        ("apps/server/Dockerfile", 30), ("apps/server/Dockerfile", 31)]
    assert "apps/server/Dockerfile:31 RUN pnpm install --frozen-lockfile" in f["evidence"]


@pytest.mark.asyncio
async def test_the_fact_reaches_the_prompt():
    entry = await _evidence(TURBO)
    prompt = ev.render_repo_evidence([entry])
    assert "BUILD/CI FACT: The shipped build installs from the committed lockfile" in prompt
    assert "does not use the committed lockfile" not in prompt


def test_a_global_cli_install_is_not_a_dependency_install():
    for seg in ("npm install -g pnpm@10.34.5", "npm i --global turbo", "pnpm add -g turbo@2",
                "yarn global add turbo", "npm install -g corepack"):
        assert ev.classify_install(seg)["global"] is True, seg
    for seg, frozen in (("pnpm install --frozen-lockfile", True), ("npm ci", True),
                        ("yarn install --immutable", True), ("yarn --frozen-lockfile", True),
                        ("pnpm install", False), ("npm install --omit dev", False), ("yarn", False)):
        c = ev.classify_install(seg)
        assert c and c["global"] is False and c["frozen"] is frozen, seg
    for seg in ("pnpm turbo run build", "yarn build", "npm install express", "pnpm run build"):
        assert ev.classify_install(seg) is None, seg


def test_instructions_keep_their_physical_line_and_stage():
    ins = ev.dockerfile_instructions(TURBO)
    by_line = {i["line"]: i for i in ins}
    assert by_line[6]["text"] == "RUN npm install -g pnpm@10.34.5" and by_line[6]["stage"] == 0
    assert by_line[24]["cmd"] == "turbo prune @example/server --docker" and by_line[24]["stage"] == 1
    assert by_line[30]["from"] == "builder" and by_line[30]["dest"] == "./pnpm-lock.yaml"
    assert by_line[31]["stage"] == 2


# ── negative: only package.json copied, plain install ────────────────────────

@pytest.mark.asyncio
async def test_package_json_only_and_a_plain_install_still_says_not_used():
    entry = await _evidence(NO_LOCK)
    [f] = _lock_findings(entry)
    assert f["kind"] == "lockfile_not_shipped", f["finding"]
    text = f["finding"]
    assert text.startswith("The shipped build does not use the committed lockfile")
    assert "copies apps/server/package.json but not pnpm-lock.yaml in any stage" in text
    assert "apps/server/Dockerfile:5 runs `pnpm install`" in text
    assert "npm install -g" not in text.split("runs `")[1].split("`")[0], "the CLI install is not the install"
    assert "zod ^4.1.0" in text and "fastify ^5.6.0" in text
    assert [(c["path"], c["line"]) for c in f["cites"]] == [("apps/server/Dockerfile", 5)]


# ── could not confirm: say so, claim neither way ─────────────────────────────

@pytest.mark.asyncio
async def test_whole_context_copy_from_a_subdirectory_is_unverified():
    """`COPY . .` from apps/server/Dockerfile: whether the root lockfile is in
    the context depends on where `docker build` runs. Plan 721's wrong answer
    came from guessing apps/server."""
    df = "FROM node:22\nWORKDIR /app\nCOPY . .\nRUN pnpm install\nCMD [\"node\", \"x.js\"]\n"
    [f] = _lock_findings(await _evidence(df))
    assert f["kind"] == "lockfile_unverified"
    assert f["finding"].startswith("Could not confirm whether the image installs from the lockfile")
    assert "line 3" in f["finding"]


@pytest.mark.asyncio
async def test_frozen_install_without_a_located_lockfile_copy_is_unverified():
    df = ("FROM node:22 AS deps\nWORKDIR /app\nCOPY apps/server/package.json ./\n"
          "RUN npm ci\nFROM node:22\nCOPY --from=deps /app/node_modules ./node_modules\n")
    [f] = _lock_findings(await _evidence(df))
    assert f["kind"] == "lockfile_unverified"
    assert "apps/server/Dockerfile:4 runs `npm ci`, which fails without a lockfile" in f["finding"]


@pytest.mark.asyncio
async def test_pruned_output_dirs_without_a_named_lockfile_are_unverified():
    """broker-wrapped: turbo prune, then only out/json/ and out/full/ are
    copied before `pnpm install --frozen-lockfile`. No line names a lockfile."""
    df = ("FROM node:22 AS base\nRUN npm install -g pnpm@10.34.5\nFROM base AS builder\nWORKDIR /app\n"
          "COPY . .\nRUN pnpm dlx turbo@2.11.11 prune @example/server --docker\nFROM base AS installer\n"
          "WORKDIR /app\nCOPY --from=builder /app/out/json/ .\nCOPY --from=builder /app/out/full/ .\n"
          "RUN pnpm install --frozen-lockfile\nFROM base AS runner\nCOPY --from=installer /app .\n")
    [f] = _lock_findings(await _evidence(df))
    assert f["kind"] == "lockfile_unverified"
    assert "fails without a lockfile" in f["finding"] and "line 9" in f["finding"]
    assert "does not use" not in f["finding"]


@pytest.mark.asyncio
async def test_a_file_from_an_external_image_raises_no_doubt():
    """OpenFaaS templates copy the watchdog binary from its own image."""
    df = ("FROM ghcr.io/openfaas/of-watchdog:0.9 AS watchdog\nFROM node:22\n"
          "COPY --from=watchdog /fwatchdog /usr/bin/fwatchdog\nWORKDIR /app\n"
          "COPY apps/server/package.json ./\nRUN npm i\n")
    [f] = _lock_findings(await _evidence(df))
    assert f["kind"] == "lockfile_not_shipped", f["finding"]


@pytest.mark.asyncio
async def test_an_install_inside_a_called_build_script_is_read():
    df = ("FROM node:22\nWORKDIR /app\nCOPY package.json pnpm-lock.yaml ./\nCOPY scripts/ scripts/\n"
          "RUN ./scripts/install.sh\n")
    script = "#!/bin/sh\nset -e\npnpm install --frozen-lockfile\n"
    entry = await _evidence(df, {"scripts/install.sh": script}, ["scripts/install.sh"])
    [f] = _lock_findings(entry)
    assert f["kind"] == "lockfile_shipped", f["finding"]
    assert "scripts/install.sh:3 runs `pnpm install --frozen-lockfile`" in f["finding"]


@pytest.mark.asyncio
async def test_an_unread_build_script_is_unverified():
    df = "FROM node:22\nWORKDIR /app\nCOPY . .\nRUN sh ./scripts/build.sh\n"
    entry = await _evidence(df, {}, ["scripts/build.sh"])  # in the tree, not readable
    [f] = _lock_findings(entry)
    assert f["kind"] == "lockfile_unverified"
    assert "scripts/build.sh" in f["finding"] and "could not be read" in f["finding"]


# ── the re-check before save ─────────────────────────────────────────────────

PLAN_721_RISK = {
    "gap": ("The server image does not install from pnpm-lock.yaml. apps/server/Dockerfile copies "
            "apps/server/package.json without the lockfile and resolves versions when the image is built."),
    "impact": "The deployed server can still contain vulnerable transitive versions.",
    "evidence": "BUILD/CI FACT: apps/server/Dockerfile (copies package.json, not pnpm-lock.yaml)",
    "kind": "other",
}
OTHER_RISK = {"gap": "Major upgrades are excluded from this PR.", "impact": "x", "kind": "other"}


def _plan(*risks) -> TestPlan:
    return TestPlan(happy_path=[], edge_cases=[], regression_checklist=[
        "Run `pnpm why next zod fastify` at 374a83d"], integration_tests=[], risks_and_gaps=list(risks))


def _facts(entry):
    return [{**f, "repo": REPO} for f in entry["build"]["findings"]]


@pytest.mark.asyncio
async def test_recheck_removes_a_lockfile_claim_the_facts_contradict():
    entry = await _evidence(TURBO)
    plan = _plan(PLAN_721_RISK, OTHER_RISK)
    facts, changes = ev.recheck_lockfile_claims(_facts(entry), plan, [entry])
    assert [f["kind"] for f in facts if f["kind"].startswith("lockfile_")] == ["lockfile_shipped"]
    assert plan.risks_and_gaps == [OTHER_RISK]
    assert [c["kind"] for c in changes] == ["risk_removed"]
    # The lockfile-based regression check is untouched.
    assert plan.regression_checklist == ["Run `pnpm why next zod fastify` at 374a83d"]


@pytest.mark.asyncio
async def test_recheck_keeps_a_claim_the_facts_support():
    entry = await _evidence(NO_LOCK)
    plan = _plan(PLAN_721_RISK)
    facts, changes = ev.recheck_lockfile_claims(_facts(entry), plan, [entry])
    assert plan.risks_and_gaps == [PLAN_721_RISK] and changes == []
    assert any(f["kind"] == "lockfile_not_shipped" for f in facts)


@pytest.mark.asyncio
async def test_recheck_downgrades_a_fact_whose_cited_lines_do_not_hold():
    entry = await _evidence(TURBO)
    # The Dockerfile as re-read no longer has the install at line 31.
    entry["build"]["cited_texts"]["apps/server/Dockerfile"] = TURBO.replace(
        "RUN pnpm install --frozen-lockfile", "RUN pnpm install")
    facts, changes = ev.recheck_lockfile_claims(_facts(entry), _plan(), [entry])
    [f] = [f for f in facts if f["kind"].startswith("lockfile_")]
    assert f["kind"] == "lockfile_unverified"
    assert "apps/server/Dockerfile:31" in f["finding"]
    assert changes[0]["kind"] == "fact_downgraded"


@pytest.mark.asyncio
async def test_recheck_removes_an_unsupported_not_used_claim_when_unverified():
    df = "FROM node:22\nWORKDIR /app\nCOPY . .\nRUN pnpm install\n"
    entry = await _evidence(df)
    plan = _plan(PLAN_721_RISK)
    _, changes = ev.recheck_lockfile_claims(_facts(entry), plan, [entry])
    assert plan.risks_and_gaps == []
    assert changes[0]["facts"] == ["lockfile_unverified"]


@pytest.mark.asyncio
async def test_a_wildcard_copy_that_matches_the_lockfile_counts():
    """partnerships-ads copies `pnpm-*.yaml`; the check read it as no lockfile."""
    df = ("FROM node:22\nWORKDIR /app\nRUN npm install -g pnpm@10.34.5\n"
          "COPY pnpm-*.yaml package.json apps/server ./\nRUN pnpm install --frozen-lockfile\n")
    [f] = _lock_findings(await _evidence(df))
    assert f["kind"] == "lockfile_shipped", f["finding"]
    assert "line 4" in f["finding"] and "apps/server/Dockerfile:5" in f["finding"]
