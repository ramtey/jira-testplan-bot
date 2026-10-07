"""Which of a ticket's PRs each test environment is actually running.

A plan that names an environment without checking it sends testers to the
wrong place. SK-2687's plan said "test on integ" with an integ tag that had
been overwritten two days earlier; a hand-run plan for the same ticket read the
deployment repo and found that staging ran neither of the two fixes, so a TC
still got a 201 there — the exact behaviour the ticket removes. Signing off on
staging would have passed a build that does not contain the change.

Where each environment's build is recorded is team-specific, so it lives in
configuration, not here (``DEPLOY_STATE_SOURCES``)::

    {"acme/web-ui": {"integ":   "acme/deployments/web-ui/integ/values.yaml",
                     "staging": "acme/deployments/web-ui/staging/values.yaml"}}

Each value is ``owner/repo/path`` to a file holding an image ``tag:`` that
ends in the commit it was built from (``release-1.96.0-6f8dbf9``,
``SK-2687-some-branch-3445b24``). A PR is in the build when that commit
contains the PR's merge commit (or, while open, its head).

Every answer is one of three, and the third is never collapsed into the
second: included, not included, could not check.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"""^\s*tag:\s*["']?([^"'\s#]+)""", re.MULTILINE)
_SHA_SUFFIX_RE = re.compile(r"[-_.]([0-9a-f]{7,40})$")


def parse_tag(values_text: str) -> str | None:
    """The first image ``tag:`` in a deployment values file."""
    m = _TAG_RE.search(values_text or "")
    return m.group(1) if m else None


def commit_of_tag(tag: str | None) -> str | None:
    """The commit a tag was built from, when the tag ends in one."""
    m = _SHA_SUFFIX_RE.search(tag or "")
    return m.group(1) if m else None


def _pr_commit(pr: dict) -> str | None:
    # A squash-merged PR's branch commits are never ancestors of a release;
    # its merge commit is. Only an open PR is identified by its head.
    if pr.get("merge_commit_sha") and (pr.get("status") or "").upper() == "MERGED":
        return pr["merge_commit_sha"]
    return pr.get("head_sha")


async def read_deploy_state(github_client, pull_requests: list[dict], sources: dict) -> list[dict]:
    """One entry per (repo, environment) that a ticket PR's repo is mapped to.

    Entry shape::

        {"repo", "env", "source", "tag", "deployed_commit", "error",
         "prs": [{"number", "url", "included": True | False | None, "detail"}]}

    ``included`` is None when it could not be checked, with ``detail`` saying
    why. ``error`` is set when the environment's build could not be read at all.
    """
    if not sources or not pull_requests or github_client is None:
        return []
    by_repo: dict[str, list[dict]] = {}
    for pr in pull_requests:
        repo = pr.get("repository")
        if repo in sources:
            by_repo.setdefault(repo, []).append(pr)

    async def one_env(repo: str, env: str, source: str) -> dict:
        entry = {"repo": repo, "env": env, "source": source, "tag": None,
                 "deployed_commit": None, "error": None, "prs": []}
        owner, name, path = (source.split("/", 2) + ["", ""])[:3]
        files = await github_client.fetch_files_at_ref(f"{owner}/{name}", None, [path])
        tag = parse_tag(files.get(path, "")) if path in files else None
        entry["tag"] = tag
        deployed = commit_of_tag(tag)
        if path not in files:
            entry["error"] = f"could not read {source}"
        elif not tag:
            entry["error"] = f"no image tag found in {source}"
        elif not deployed:
            entry["error"] = f"tag {tag} does not name a commit"
        entry["deployed_commit"] = deployed

        for pr in by_repo[repo]:
            item = {"number": pr.get("number"), "url": pr.get("url"), "included": None, "detail": ""}
            pr_commit = _pr_commit(pr)
            if entry["error"]:
                item["detail"] = "environment's build could not be read"
            elif not pr_commit:
                item["detail"] = "PR has no commit to compare"
            else:
                cmp = await github_client.compare_commits(repo, pr_commit, deployed)
                if cmp is None:
                    item["detail"] = "GitHub compare failed"
                elif cmp["status"] in ("identical", "ahead"):
                    item["included"] = True
                elif cmp["status"] == "behind" and (pr.get("status") or "").upper() != "MERGED":
                    # The build is an ancestor of the open PR's head: an older
                    # revision of this PR (or the base before it).
                    item["included"] = False
                    item["detail"] = (f"build predates the PR head by {cmp['behind_by']} "
                                      "commit(s) — it holds an older revision of the PR, or none of it")
                else:
                    item["included"] = False
            entry["prs"].append(item)
        return entry

    jobs = [one_env(repo, env, src)
            for repo in by_repo for env, src in sources[repo].items()]
    try:
        return list(await asyncio.gather(*jobs))
    except Exception as e:  # never let a deployment read break plan generation
        logger.warning("deploy state read failed: %s", e)
        return [{"repo": r, "env": "*", "source": "", "tag": None, "deployed_commit": None,
                 "error": f"deploy state read failed: {type(e).__name__}", "prs": []}
                for r in by_repo]


def render_deploy_state(entries: list[dict], now: datetime | None = None) -> str:
    """The prompt section. Empty when nothing was configured for this ticket."""
    if not entries:
        return ""
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Read from the deployment config at {when}. Builds change; this is a snapshot."]
    for e in entries:
        head = f"- {e['env']} · {e['repo']}"
        if e["error"]:
            lines.append(f"{head}: COULD NOT CHECK ({e['error']}). Do not claim this "
                         "environment has or lacks any PR.")
            continue
        lines.append(f"{head}: runs {e['tag']}")
        for p in e["prs"]:
            label = {True: "INCLUDED", False: "NOT INCLUDED", None: "COULD NOT CHECK"}[p["included"]]
            detail = f" — {p['detail']}" if p["detail"] else ""
            lines.append(f"    #{p['number']}: {label}{detail}")
    return "\n".join(lines)


DEPLOY_STATE_GUIDANCE = (
    "Use the deployment state above to choose each case's environment: name one "
    "where every PR the case depends on is INCLUDED. If no environment has them "
    "all, say which PR is missing where, and mark the case blocked on deployment "
    "rather than pointing a tester at a build that lacks the change. Never "
    "recommend sign-off on an environment marked NOT INCLUDED for a PR the "
    "ticket depends on. COULD NOT CHECK means unknown — say so; it is not "
    "evidence either way."
)
