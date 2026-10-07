"""Which environment runs which PR.

Why this file exists
--------------------
SK-2687's generated plan pointed testers at integ by a tag that had been
overwritten, and said nothing about staging — which ran neither fix, so a TC
still got a 201 there. A hand-run plan caught it by reading the deployment repo.

The rule these tests hold above all: a read that failed is "could not check",
never "not included". That is the repo's recurring bug class (a transient
failure stated as fact), and here it would tell a tester a fix is missing from
a build that has it.
"""

from __future__ import annotations

import pytest

from src.app.deploy_state import (
    commit_of_tag,
    parse_tag,
    read_deploy_state,
    render_deploy_state,
)

VALUES = """# Environment specific configuration values
skyslope-app:
  image:
    tag: releasev1.96.0-6f8dbf9
  env: staging
"""
SOURCES = {"acme/api": {"integ": "acme/deploys/api/integ/values.yaml",
                        "staging": "acme/deploys/api/staging/values.yaml"}}
MERGED = {"repository": "acme/api", "number": 92, "url": "u92", "status": "MERGED",
          "head_sha": "branchtip", "merge_commit_sha": "squash92"}
OPEN = {"repository": "acme/api", "number": 96, "url": "u96", "status": "OPEN",
        "head_sha": "head96"}


class FakeGitHub:
    def __init__(self, files, compares):
        self.files, self.compares, self.compared = files, compares, []

    async def fetch_files_at_ref(self, repo, ref, paths):
        return {p: self.files[f"{repo}/{p}"] for p in paths if f"{repo}/{p}" in self.files}

    async def compare_commits(self, repo, base, head):
        self.compared.append((base, head))
        return self.compares.get((base, head))


def test_parse_tag_and_commit():
    assert parse_tag(VALUES) == "releasev1.96.0-6f8dbf9"
    assert commit_of_tag("SK-2687-hide-in-person-from-delegates-3445b24") == "3445b24"
    assert commit_of_tag("latest") is None
    assert parse_tag("image:\n  repository: x\n") is None


@pytest.mark.asyncio
async def test_included_not_included_and_merge_commit_used():
    gh = FakeGitHub(
        files={"acme/deploys/api/integ/values.yaml": VALUES.replace("6f8dbf9", "59b1180"),
               "acme/deploys/api/staging/values.yaml": VALUES},
        compares={("squash92", "59b1180"): {"status": "ahead", "ahead_by": 3, "behind_by": 0},
                  ("squash92", "6f8dbf9"): {"status": "diverged", "ahead_by": 2, "behind_by": 5}},
    )
    state = await read_deploy_state(gh, [MERGED], SOURCES)
    by_env = {e["env"]: e for e in state}
    assert by_env["integ"]["prs"][0]["included"] is True
    assert by_env["staging"]["prs"][0]["included"] is False
    # A squash merge is found by its merge commit, never the branch tip.
    assert all(base == "squash92" for base, _ in gh.compared)


@pytest.mark.asyncio
async def test_open_pr_with_an_older_revision_deployed():
    gh = FakeGitHub(
        files={"acme/deploys/api/integ/values.yaml": VALUES.replace("6f8dbf9", "abc1234")},
        compares={("head96", "abc1234"): {"status": "behind", "ahead_by": 0, "behind_by": 2}},
    )
    state = await read_deploy_state(gh, [OPEN], {"acme/api": {"integ": SOURCES["acme/api"]["integ"]}})
    item = state[0]["prs"][0]
    assert item["included"] is False
    assert "older revision of the PR, or none of it" in item["detail"]


@pytest.mark.asyncio
async def test_unreadable_build_is_could_not_check_not_missing():
    gh = FakeGitHub(files={}, compares={})
    state = await read_deploy_state(gh, [MERGED], SOURCES)
    assert all(e["error"] for e in state)
    assert all(p["included"] is None for e in state for p in e["prs"])
    text = render_deploy_state(state)
    assert "COULD NOT CHECK" in text
    assert "NOT INCLUDED" not in text


@pytest.mark.asyncio
async def test_failed_compare_is_could_not_check():
    gh = FakeGitHub(files={"acme/deploys/api/staging/values.yaml": VALUES}, compares={})
    state = await read_deploy_state(gh, [MERGED], {"acme/api": {"staging": SOURCES["acme/api"]["staging"]}})
    assert state[0]["prs"][0]["included"] is None
    assert "#92: COULD NOT CHECK" in render_deploy_state(state)


@pytest.mark.asyncio
async def test_unmapped_repo_or_no_config_reads_nothing():
    gh = FakeGitHub(files={}, compares={})
    assert await read_deploy_state(gh, [dict(MERGED, repository="acme/other")], SOURCES) == []
    assert await read_deploy_state(gh, [MERGED], {}) == []


def test_render_names_tag_and_verdicts():
    state = [{"repo": "acme/api", "env": "staging", "source": "s", "tag": "releasev1.96.0-6f8dbf9",
              "deployed_commit": "6f8dbf9", "error": None,
              "prs": [{"number": 92, "url": "u", "included": False, "detail": ""}]}]
    text = render_deploy_state(state)
    assert "staging · acme/api: runs releasev1.96.0-6f8dbf9" in text
    assert "#92: NOT INCLUDED" in text
    assert render_deploy_state([]) == ""
