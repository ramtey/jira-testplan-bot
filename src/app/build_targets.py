"""Which build a case runs against, and how a tester proves it holds the change.

Evidence rule B puts a check in front of every case that needs a running
build. Until this module that check was one sentence for every case on every
ticket: "confirm this environment runs merged commit <the newest merge> — read
the deployment repo's image tag, the ArgoCD app's image, or the running pod's
image". Two plans showed what that costs, and none of it is specific to them:

* **One commit for every repo.** A ticket that changed a backend and an iOS app
  named the iOS merge commit in the backend's deploy check, because the newest
  merge across all PRs won. A correctly deployed backend would read as blocked.
  Each repo has its own merge commit, and a case is checked against the repos
  it exercises.
* **An exact tag instead of containment.** The plan copied the image tag from a
  deployments-repo PR (a deploy that happened once) into the case. The
  environment was by then running a later master build that contained the
  merge, which is correct. A build passes when its commit *contains* the merge
  commit, an ancestor check, whatever the tag is called.
* **The wrong platform.** Every case of an iOS-only change started by checking a
  container image or an ArgoCD app, neither of which exists for an iOS app. An
  app build is checked by the installed app's version and the commit it was
  built from.

This module decides, per repo, what platform it ships on and how a tester reads
what is running, and builds the check step for a case from the repos that case
exercises. It also finds where builds and pipelines are recorded when no one
configured it: a deployments repo (a ticket PR into one, ``DEPLOYMENTS_REPOS``,
or the repos ``DEPLOY_STATE_SOURCES`` already points at) holds each service's
per-environment values file and, on some teams, its CI pipeline.

As everywhere in the evidence checks, "could not read" stays distinct from
"no": a value this module could not find is left out of the step, never guessed.
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Iterable

logger = logging.getLogger(__name__)

PLATFORMS = ("backend", "ios", "android", "web", "unknown")

#: Surfaces map to the platforms a case on them can exercise.
_SURFACE_PLATFORMS = {
    "mobile": ("ios", "android"),
    "backend_http": ("backend",),
    "web_ui": ("web",),
}


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------

_WEB_MARKERS = re.compile(
    r"(^|/)(next\.config\.[cm]?[jt]s|vite\.config\.[cm]?[jt]s|angular\.json|svelte\.config\.[jt]s"
    r"|nuxt\.config\.[jt]s|remix\.config\.[jt]s|astro\.config\.mjs|gatsby-config\.[jt]s"
    r"|public/index\.html|src/index\.html)$"
)
_BACKEND_MARKERS = re.compile(
    r"(^|/)(Dockerfile[^/]*|serverless\.ya?ml|Procfile|go\.mod|pom\.xml|[^/]+\.csproj|manage\.py"
    r"|stack\.ya?ml|template\.ya?ml)$"
)


def detect_platform(tree: Iterable[str], changed: Iterable[str] = ()) -> str:
    """``ios``, ``android``, ``web``, ``backend`` or ``unknown`` for a repo.

    Read from the repo tree when there is one, else from the changed files.
    An app project wins over a Dockerfile: an iOS repo with a tooling image is
    still shipped to testers as an app build.
    """
    paths = list(tree) or list(changed)
    changed = list(changed)
    if any(p.endswith(".xcodeproj/project.pbxproj") or posixpath.basename(p) in ("project.yml", "Package.swift")
           for p in paths) and any(p.endswith((".swift", ".m", ".mm")) for p in paths + changed):
        return "ios"
    if not tree and any(p.endswith((".swift", ".m", ".xib", ".storyboard")) for p in changed):
        return "ios"
    if any(posixpath.basename(p) == "AndroidManifest.xml" for p in paths):
        return "android"
    if any(_WEB_MARKERS.search(p) for p in paths):
        return "web"
    if any(_BACKEND_MARKERS.search(p) for p in paths):
        return "backend"
    if any(p.endswith((".tsx", ".jsx", ".vue", ".svelte")) for p in changed):
        return "web"
    if any(p.endswith((".ts", ".js", ".py", ".go", ".cs", ".java", ".rb")) for p in changed):
        return "backend"
    return "unknown"


# ---------------------------------------------------------------------------
# App versions — what an app build at the merge commit reports
# ---------------------------------------------------------------------------

_VERSION_KEY = re.compile(r"\bMARKETING_VERSION\s*[=:]\s*[\"']?([0-9][0-9A-Za-z.\-]*)")
_BUILD_KEY = re.compile(r"\bCURRENT_PROJECT_VERSION\s*[=:]\s*[\"']?([0-9][0-9A-Za-z.\-]*)")
_BUNDLE_KEY = re.compile(r"\bPRODUCT_BUNDLE_IDENTIFIER\s*[=:]\s*[\"']?([A-Za-z0-9.\-_$(){}]+)")
_GRADLE_NAME = re.compile(r"\bversionName\s*=?\s*[\"']([^\"']+)[\"']")
_GRADLE_CODE = re.compile(r"\bversionCode\s*=?\s*(\d+)")
_GRADLE_APP_ID = re.compile(r"\bapplicationId\s*=?\s*[\"']([^\"']+)[\"']")


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-]", v))


def version_files(tree: Iterable[str], platform: str) -> list[str]:
    """The files an app's version is set in, most authoritative first."""
    tree = list(tree)
    if platform == "ios":
        # project.pbxproj is what Xcode builds and what fastlane's
        # get_version_number reads; project.yml (XcodeGen) is its source when
        # the project is generated, and may only carry a base value.
        pbx = sorted((p for p in tree if p.endswith(".xcodeproj/project.pbxproj")
                      and "Pods/" not in p), key=len)
        yml = sorted((p for p in tree if posixpath.basename(p) == "project.yml"), key=len)
        return pbx[:1] + yml[:1]
    if platform == "android":
        return sorted((p for p in tree if re.search(r"(^|/)app/build\.gradle(\.kts)?$", p)), key=len)[:1] or \
            sorted((p for p in tree if posixpath.basename(p) in ("build.gradle", "build.gradle.kts")), key=len)[:2]
    if platform == "web":
        return ["package.json"] if "package.json" in tree else []
    return []


def parse_app_version(platform: str, files: dict[str, str]) -> dict | None:
    """``{"version", "build", "bundle_ids", "file"}`` from the files read, or None.

    For iOS the highest MARKETING_VERSION in the first file that sets one: test
    and extension targets often keep Xcode's 1.0.0 beside the app's real one.
    """
    for path, text in files.items():
        text = text or ""
        if platform == "ios":
            versions = [v for v in _VERSION_KEY.findall(text) if "$" not in v]
            if not versions:
                continue
            builds = [b for b in _BUILD_KEY.findall(text) if "$" not in b]
            bundles = sorted({b.strip("\"'") for b in _BUNDLE_KEY.findall(text)
                              if "$" not in b and not re.search(r"tests?$", b, re.I)})
            return {"version": max(versions, key=_vkey),
                    "build": max(builds, key=_vkey) if builds else None,
                    "bundle_ids": bundles, "file": path}
        if platform == "android":
            name = _GRADLE_NAME.search(text)
            if not name:
                continue
            code = _GRADLE_CODE.search(text)
            app = _GRADLE_APP_ID.search(text)
            return {"version": name.group(1), "build": code.group(1) if code else None,
                    "bundle_ids": [app.group(1)] if app else [], "file": path}
        if platform == "web":
            m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
            if m:
                return {"version": m.group(1), "build": None, "bundle_ids": [], "file": path}
    return None


# ---------------------------------------------------------------------------
# Deployments repos — where builds and pipelines are recorded
# ---------------------------------------------------------------------------

_DEPLOY_FILE = re.compile(r"(^|/)(values[^/]*\.ya?ml|Chart\.ya?ml|kustomization\.ya?ml|[^/]+\.tfvars)$")


def short_name(repo: str) -> str:
    return (repo or "").rsplit("/", 1)[-1]


def is_deployments_repo(repo: str, changed: Iterable[str], configured: Iterable[str] = ()) -> bool:
    """A repo that records deployments rather than holding code that ships.

    Configured repos always are. Otherwise a PR into it changes only
    deployment files (values files, charts, kustomizations, tfvars), or the
    repo is named for it (``*-deployments``, ``gitops``).
    """
    if repo in set(configured):
        return True
    name = short_name(repo).lower()
    if re.search(r"deploy|gitops|argocd|helm-?charts", name):
        return True
    changed = [c for c in changed if c]
    return bool(changed) and all(_DEPLOY_FILE.search(c) for c in changed)


def find_deploy_values(tree: Iterable[str], repo_name: str) -> dict[str, str]:
    """``{env: path}`` for a service's per-environment values files.

    The common layout is ``<team>/<service>/<env>/values.yaml``. Pipeline
    definitions sit under a ``pipelines`` directory and are not deployments.
    """
    out: dict[str, str] = {}
    pat = re.compile(rf"(?:^|/){re.escape(repo_name)}/([^/]+)/values\.ya?ml$")
    for p in tree:
        if "/pipelines/" in f"/{p}" or "/_" in f"/{p}":
            continue
        m = pat.search(p)
        if m:
            out.setdefault(m.group(1), p)
    return dict(sorted(out.items()))


def find_pipeline_files(tree: Iterable[str], repo_name: str) -> list[str]:
    """CI pipeline definitions for a service kept in a deployments repo.

    ``<team>/pipelines/<service>/values.yaml`` (an Argo Workflows pipeline
    chart), or any YAML under a ``pipelines``/``workflows`` directory named for
    the service.
    """
    pat = re.compile(rf"(?:^|/)(?:pipelines|workflows|ci)/(?:[^/]+/)*{re.escape(repo_name)}"
                     rf"(?:/[^/]+|\.ya?ml)$")
    return sorted(p for p in tree if pat.search(p) and p.endswith((".yaml", ".yml")))


def names_repo(text: str, repo_name: str) -> bool:
    """Whether a pipeline file is triggered by ``repo_name`` (not just near it)."""
    return bool(re.search(rf"(?:repoName|repo|repository|repo_name)\s*:\s*[\"']?(?:[\w.-]+/)?"
                          rf"{re.escape(repo_name)}[\"']?\s*$", text or "", re.M))


async def discover_deployments(github, pull_requests: list[dict] | None, *,
                               configured_sources: dict | None = None,
                               deployments_repos: Iterable[str] = ()) -> dict:
    """Deploy sources and CI pipelines for the ticket's code repos.

    Returns::

        {"deployments_repos": [...],
         "deploy_sources": {code_repo: {env: "owner/repo/path"}},
         "pipelines": {code_repo: [{"source": "owner/repo/path", "text": str}]},
         "errors": [str]}

    Configured ``DEPLOY_STATE_SOURCES`` win for a repo they name; discovery
    only fills repos nobody configured. A deployments repo whose tree could not
    be listed is an error, never "nothing deployed".
    """
    configured_sources = configured_sources or {}
    out = {"deployments_repos": [], "deploy_sources": {}, "pipelines": {}, "errors": []}
    if github is None:
        return out
    by_repo: dict[str, list[str]] = {}
    for pr in pull_requests or []:
        if isinstance(pr, dict) and pr.get("repository"):
            by_repo.setdefault(pr["repository"], []).extend(
                f.get("filename") for f in (pr.get("files_changed") or [])
                if isinstance(f, dict) and f.get("filename"))
    configured_repos = set(deployments_repos) | {
        "/".join(src.split("/", 2)[:2]) for envs in configured_sources.values()
        for src in (envs or {}).values() if src.count("/") >= 2}
    deploy_repos = sorted(configured_repos | {r for r, ch in by_repo.items()
                                              if is_deployments_repo(r, ch, configured_repos)})
    code_repos = [r for r in by_repo if r not in deploy_repos]
    out["deployments_repos"] = deploy_repos
    if not code_repos or not deploy_repos:
        return out
    for drepo in deploy_repos:
        tree = await github.list_tree(drepo, "HEAD")
        if tree is None:
            out["errors"].append(f"could not list {drepo}, so deployments and pipelines recorded "
                                 "there were not checked")
            continue
        pipeline_paths: dict[str, list[str]] = {}
        for code in code_repos:
            name = short_name(code)
            if code not in configured_sources:
                found = find_deploy_values(tree, name)
                if found:
                    srcs = out["deploy_sources"].setdefault(code, {})
                    for env, path in found.items():
                        srcs.setdefault(env, f"{drepo}/{path}")
            paths = find_pipeline_files(tree, name)
            if paths:
                pipeline_paths[code] = paths[:4]
        wanted = sorted({p for ps in pipeline_paths.values() for p in ps})
        texts = await github.fetch_files_at_ref(drepo, None, wanted) if wanted else {}
        for code, paths in pipeline_paths.items():
            for p in paths:
                if p not in texts:
                    out["errors"].append(f"could not read {drepo}/{p}, a pipeline that may run "
                                         f"{code}'s tests")
                    continue
                if names_repo(texts[p], short_name(code)) or f"/{short_name(code)}/" in f"/{p}":
                    out["pipelines"].setdefault(code, []).append(
                        {"source": f"{drepo}/{p}", "text": texts[p]})
    return out


# ---------------------------------------------------------------------------
# Targets — one per code repo
# ---------------------------------------------------------------------------

def merge_commit_for(prs: list[dict]) -> str | None:
    """The repo's own merge commit: its newest merged PR's, else an open head."""
    merged = [p for p in prs if (p.get("status") or "").upper() == "MERGED" and p.get("merge_commit_sha")]
    if merged:
        return max(merged, key=lambda p: p.get("merged_at") or "")["merge_commit_sha"]
    return next((p.get("head_sha") for p in prs if p.get("head_sha")), None)


def build_targets(pull_requests: list[dict] | None, repo_evidence: list[dict] | None = None,
                  deploy_state: list[dict] | None = None,
                  deployments_repos: Iterable[str] = ()) -> list[dict]:
    """One target per code repo the ticket changed::

        {"repo", "commit", "platform", "changed": [paths], "app": parse_app_version(...),
         "deploy": [{"env", "source", "tag", "deployed_commit", "included", "error"}]}

    Deployments repos are not targets: nothing a tester runs is built from them.
    """
    deployments_repos = set(deployments_repos)
    by_repo: dict[str, list[dict]] = {}
    for pr in pull_requests or []:
        if isinstance(pr, dict) and pr.get("repository"):
            by_repo.setdefault(pr["repository"], []).append(pr)
    evidence = {e.get("repo"): e for e in repo_evidence or [] if isinstance(e, dict)}
    targets = []
    for repo, prs in by_repo.items():
        changed = sorted({f.get("filename") for pr in prs for f in (pr.get("files_changed") or [])
                          if isinstance(f, dict) and f.get("filename")})
        if repo in deployments_repos or is_deployments_repo(repo, changed):
            continue
        e = evidence.get(repo) or {}
        commit = merge_commit_for(prs) or e.get("ref")
        platform = e.get("platform") or detect_platform((), changed)
        deploy = []
        for d in deploy_state or []:
            if d.get("repo") != repo:
                continue
            inc = [p.get("included") for p in d.get("prs") or []]
            deploy.append({"env": d.get("env"), "source": d.get("source"), "tag": d.get("tag"),
                           "deployed_commit": d.get("deployed_commit"), "error": d.get("error"),
                           "included": (None if not inc or None in inc else all(inc))})
        targets.append({"repo": repo, "commit": commit, "platform": platform, "changed": changed,
                        "app": e.get("app_version"), "deploy": deploy})
    return targets


# ---------------------------------------------------------------------------
# Which repos a case exercises
# ---------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:[\w@.+-]+/)*[\w@+-][\w.+-]*\.[A-Za-z]{1,6})(?::\d+(?:-\d+)?)?")


def cited_paths(text: str) -> list[str]:
    """File paths named in free text (``src/a.ts:12``, ``SceneDelegate.swift``)."""
    return [m.group(1) for m in _PATH_TOKEN.finditer(text or "")
            if not re.match(r"^\d+(\.\d+)+$", m.group(1)) and not m.group(1).startswith(("e.g", "i.e"))]


def path_matches(cited: str, changed: str) -> bool:
    """A cited path names a changed file: equal, or one is a path suffix of the other."""
    cited, changed = cited.strip("`'\" ").lstrip("./"), changed.lstrip("./")
    if not cited or not changed:
        return False
    if cited == changed:
        return True
    if "/" in cited:
        return changed.endswith("/" + cited) or cited.endswith("/" + changed)
    return posixpath.basename(changed) == cited


def _case_paths(case: dict) -> list[str]:
    paths = []
    for g in case.get("grounded_in") or []:
        if isinstance(g, str) and g.startswith("pr:"):
            paths.append(g[3:])
    for k in ("expected_source", "expected", "preconditions", "trigger", "unit_test_ref"):
        if isinstance(case.get(k), str):
            paths += cited_paths(case[k])
    for s in case.get("steps") or []:
        if isinstance(s, str):
            paths += cited_paths(s)
    return paths


def _distinct(name: str) -> bool:
    """A repo name that will not match ordinary prose (`mobile-api`, not `web`)."""
    return len(name) >= 8 or bool(re.search(r"[-_.]", name)) and len(name) >= 5


def targets_for_case(case: dict, targets: list[dict]) -> list[dict]:
    """The repos a case exercises, the one its surface runs on first.

    From the files the case cites (``grounded_in``, ``expected_source``, step
    text) matched against each repo's changed files, then from its surface. A
    case that cites nothing on a ticket with one repo exercises that repo.
    """
    if len(targets) <= 1:
        return list(targets)
    paths = _case_paths(case)
    text = " ".join(str(v) for k, v in case.items() if k in (
        "title", "preconditions", "environment", "expected", "trigger") and isinstance(v, str))
    text += " " + " ".join(s for s in case.get("steps") or [] if isinstance(s, str))
    # A case that names a repo ("the mobile-api integ deployment") exercises it
    # even when every file it cites is in another one.
    hit = [t for t in targets if any(path_matches(p, c) for p in paths for c in t["changed"])
           or (_distinct(short_name(t["repo"]))
               and re.search(rf"(?<![\w/-]){re.escape(short_name(t['repo']))}(?![\w-])", text))]
    surface = _SURFACE_PLATFORMS.get(case.get("surface") or "", ())
    on_surface = [t for t in targets if t["platform"] in surface]
    if hit:
        return sorted(hit, key=lambda t: t["platform"] not in surface)
    return on_surface or list(targets)


# ---------------------------------------------------------------------------
# The check step
# ---------------------------------------------------------------------------

def _sha(commit: str | None) -> str:
    return (commit or "")[:7]


def _compare_cmd(repo: str, sha: str, deployed: str = "<deployed commit>") -> str:
    return f"`gh api repos/{repo}/compare/{sha}...{deployed} --jq .status`"


def _container_check(t: dict, when: str | None) -> str:
    sha = _sha(t["commit"])
    what = f"merge commit {sha}" if sha else "the merged change"
    readable = [d for d in t["deploy"] if d.get("source") and not d.get("error")]
    if readable:
        where = " or ".join(sorted({d["source"] for d in readable}))
        how = (f"read the image tag in {where} (the tag ends in the commit it was built from, "
               "e.g. `master-<sha>`)")
    else:
        how = ("read the deployed commit from this environment's deployment values file "
               "(the image tag ends in the commit it was built from), the ArgoCD app's image, "
               "or the running pod's image")
    status = []
    for d in t["deploy"]:
        if d.get("error"):
            status.append(f"{d['env']}: could not check ({d['error']})")
        elif d.get("included") is True:
            status.append(f"{d['env']} runs {d['tag']}, which contains it")
        elif d.get("included") is False:
            status.append(f"{d['env']} runs {d['tag']}, which does NOT contain it")
        elif d.get("tag"):
            status.append(f"{d['env']} runs {d['tag']} (containment not checked)")
    snapshot = f" As of {when}: {'; '.join(status)}." if status and when else (
        f" Last read: {'; '.join(status)}." if status else "")
    return (f"{t['repo']} — confirm the deployed image contains {what}: {how}, then run "
            f"{_compare_cmd(t['repo'], sha or '<merge commit>')}; `ahead` or `identical` means "
            f"the build contains the merge, `behind` or `diverged` means it does not. Any later "
            f"build that contains it passes; the tag does not have to match a particular name."
            f"{snapshot}")


def _ios_check(t: dict) -> str:
    sha = _sha(t["commit"])
    app = t.get("app") or {}
    bundle = (app.get("bundle_ids") or ["<bundle id>"])
    bundle_hint = bundle[0] if len(bundle) == 1 else f"<bundle id: one of {', '.join(bundle[:4])}>"
    if app.get("version"):
        floor = (f"CFBundleShortVersionString must be at least {app['version']} (MARKETING_VERSION in "
                 f"{app['file']} at {sha})")
        if app.get("build"):
            floor += f" and CFBundleVersion at least {app['build']} (CURRENT_PROJECT_VERSION there)"
    else:
        floor = (f"CFBundleShortVersionString and CFBundleVersion must be at least the version "
                 f"MARKETING_VERSION / CURRENT_PROJECT_VERSION (project.pbxproj or project.yml) "
                 f"hold at {sha or 'the merge commit'} — the version could not be read when this plan was written")
    return (f"{t['repo']} (iOS app; there is no image or ArgoCD app to check) — confirm the installed "
            f"build contains merge commit {sha}: read its version with `plutil -p \"$(xcrun simctl "
            f"get_app_container booted {bundle_hint})/Info.plist\" | grep -E "
            f"'CFBundleShortVersionString|CFBundleVersion'` (simulator; `xcrun simctl listapps booted` "
            f"lists every installed app with both keys), or the app's About screen / TestFlight build on a "
            f"device. {floor}, and the build must come from a commit that contains {sha}: the TestFlight "
            f"build's source commit, or for a simulator build `git merge-base --is-ancestor {sha} HEAD` in "
            f"the checkout it was built from (exit 0 means it contains the merge).")


def _android_check(t: dict) -> str:
    sha = _sha(t["commit"])
    app = t.get("app") or {}
    pkg = (app.get("bundle_ids") or ["<application id>"])[0]
    floor = (f"versionName must be at least {app['version']}"
             + (f" and versionCode at least {app['build']}" if app.get("build") else "")
             + f" ({app['file']} at {sha})") if app.get("version") else (
        f"versionName / versionCode must be at least what build.gradle holds at {sha}")
    return (f"{t['repo']} (Android app; there is no image to check) — confirm the installed build "
            f"contains merge commit {sha}: `adb shell dumpsys package {pkg} | grep -E "
            f"'versionName|versionCode'`. {floor}, and the build must come from a commit that contains "
            f"{sha} (`git merge-base --is-ancestor {sha} <build commit>`).")


def _web_check(t: dict, when: str | None) -> str:
    if t["deploy"]:
        return _container_check(t, when)
    sha = _sha(t["commit"])
    app = t.get("app") or {}
    ver = f" The bundle's version must be at least {app['version']} ({app['file']} at {sha})." if app.get("version") else ""
    return (f"{t['repo']} (web app) — confirm the served build contains merge commit {sha}: read the "
            f"commit the deployed bundle was built from (the app's version endpoint or build metadata, "
            f"or the image tag if it ships as a container), then run "
            f"{_compare_cmd(t['repo'], sha or '<merge commit>')}; `ahead` or `identical` passes.{ver}")


def check_clause(t: dict, when: str | None = None) -> str:
    """How a tester confirms the build they use contains this repo's merge."""
    if t["platform"] == "ios":
        return _ios_check(t)
    if t["platform"] == "android":
        return _android_check(t)
    if t["platform"] == "web":
        return _web_check(t, when)
    return _container_check(t, when)


def build_check_step(targets: list[dict], when: str | None = None) -> str:
    """The first step of a case that needs a running build."""
    clauses = [check_clause(t, when) for t in targets]
    body = clauses[0] if len(clauses) == 1 else " ".join(
        f"({n}) {c}" for n, c in enumerate(clauses, 1))
    return (f"Build check: {body} If it does not, stop: the case is blocked on "
            f"{'a build' if all(t['platform'] in ('ios', 'android') for t in targets) else 'deployment'}.")


def short_requirement(targets: list[dict]) -> str:
    """One line for a checklist prefix: what each repo's build must contain."""
    parts = []
    for t in targets:
        sha = _sha(t["commit"]) or "its merge"
        if t["platform"] in ("ios", "android"):
            ver = (t.get("app") or {}).get("version")
            parts.append(f"the installed {short_name(t['repo'])} app is built from a commit containing {sha}"
                         + (f" (version {ver} or later)" if ver else ""))
        else:
            parts.append(f"the deployed {short_name(t['repo'])} build contains {sha}")
    return "; ".join(parts)


#: An exact image tag pinned as the thing to find: ``image tag
#: SK-2719-in-person-session-4a6351a``, ``(image master-12414e5)``.
_PINNED_TAG_RE = re.compile(
    r"\b(?:running\s+)?(?:image(?:\s+tag)?|tag)\s+`?([A-Za-z0-9][A-Za-z0-9._-]*[-_.][0-9a-f]{7,40})`?",
    re.I,
)


def unpin_tags(text: str, targets: list[dict]) -> tuple[str, int]:
    """Replace an exact image tag a case demands with "a build that contains <merge>".

    The tag a case copies is a deploy that happened once (often the
    deployments-repo PR the ticket links); a later build that contains the
    merge is just as correct, and the exact-tag form would call it blocked.
    """
    shas = [_sha(t["commit"]) for t in targets if t["platform"] not in ("ios", "android") and t["commit"]]
    if not text or not shas:
        return text, 0
    what = "a build that contains merge commit " + " / ".join(shas)
    return _PINNED_TAG_RE.subn(what, text)
