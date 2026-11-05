"""The repo's real commands and identifiers (rule Y), and the ticket's "before" commit (rule T).

A UAT run on an Android ticket had a plan that told the tester to run
``gradle test`` and ``adb shell dumpsys package <applicationId>``. The repo
builds with its wrapper (``./gradlew :app:test<Variant>UnitTest``) and the debug
build the tester installs carries an ``applicationIdSuffix``, so both commands
were wrong before anyone ran them. Nothing in the plan pipeline had read the
build config for variants, and the build check itself used the bare
``applicationId``.

This module reads, per repo and at the commit under test:

* **Android:** every build type and product flavour in the app module's
  ``build.gradle(.kts)``, each variant's real application id (base id, flavour
  override, flavour suffix, build-type suffix), the module's Gradle path, and
  whether a Gradle wrapper is committed.
* **Node:** the ``package.json`` script names and the package manager the
  lockfile implies.
* **iOS:** the bundle ids ``build_targets`` already parses.

``render_command_facts`` gives the generator these as facts, and
``check_commands`` re-reads every case and regression line before save: a
command or identifier that the build config contradicts is marked where it
stands, with what the config has instead. Unknown is never wrong: a build file
that could not be read, or variants spread over several flavour dimensions,
make the variant check stand down rather than flag.

``ticket_base`` finds the commit "before this change" means: the merge base of
the ticket's first PR in each repo, not an intermediate commit of the same PRs.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Android: build types, flavours, variant application ids
# ---------------------------------------------------------------------------

_APP_ID_RE = re.compile(r"\bapplicationId\s*=?\s*[\"']([^\"']+)[\"']")
_SUFFIX_RE = re.compile(r"\bapplicationIdSuffix\s*=?\s*[\"']([^\"']+)[\"']")
_BLOCK_NAME_RE = re.compile(
    r"(?:\b(?:getByName|create|register|maybeCreate|named)\s*\(\s*[\"'](\w+)[\"']\s*\)|\b(\w+))\s*(?:\{|\.\s*(?:configure|apply)\s*\{)"
)
_DIMENSIONS_RE = re.compile(r"\bflavorDimensions\s*(?:\+=|=|\()?\s*(?:listOf\s*\()?([^\n)]*)")
_NOT_A_VARIANT = {"defaultConfig", "signingConfigs", "signingConfig", "buildFeatures", "packaging",
                  "packagingOptions", "lint", "testOptions", "kotlinOptions", "compileOptions",
                  "if", "else", "all", "configureEach", "each", "forEach", "withType", "named",
                  "apply", "configure", "also", "let", "run", "with"}


def _block(text: str, start: int) -> tuple[str, int]:
    """The ``{ … }`` body that opens at or after ``start``; and the index after it."""
    i = text.find("{", start)
    if i < 0:
        return "", start
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
    return text[i + 1:], len(text)


def _named_block(text: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*\{{", text)
    return _block(text, m.start())[0] if m else None


def _children(body: str) -> dict[str, str]:
    """``{name: block}`` for the top-level named blocks inside ``body``."""
    out: dict[str, str] = {}
    i = 0
    while i < len(body):
        m = _BLOCK_NAME_RE.search(body, i)
        if not m:
            break
        name = m.group(1) or m.group(2)
        inner, end = _block(body, m.start())
        if name and name not in _NOT_A_VARIANT:
            out[name] = inner
        i = max(end, m.end())
    return out


def _cap(name: str) -> str:
    return name[:1].upper() + name[1:]


def parse_android_variants(path: str, text: str) -> dict | None:
    """``{"file", "module", "application_id", "variants": [{"name", "application_id"}],
    "complete": bool}`` from one module's build file, or None when it sets no applicationId.

    ``complete`` is False when the variant list may be missing some (several
    flavour dimensions, a suffix set from code), so a checker must not call
    an unlisted variant wrong.
    """
    if not text:
        return None
    android = _named_block(text, "android") or text
    default = _named_block(android, "defaultConfig") or ""
    base_m = _APP_ID_RE.search(default) or _APP_ID_RE.search(android)
    if not base_m:
        return None
    base = base_m.group(1)
    complete = "$" not in base
    build_types = {"debug": "", "release": ""}
    bt_body = _named_block(android, "buildTypes")
    if bt_body:
        for name, body in _children(bt_body).items():
            m = _SUFFIX_RE.search(body)
            build_types[name] = m.group(1) if m else ""
            if re.search(r"\bapplicationIdSuffix\b", body) and not m:
                complete = False  # set from code, not a literal
    flavors: dict[str, tuple[str, str]] = {}
    fl_body = _named_block(android, "productFlavors")
    if fl_body:
        for name, body in _children(fl_body).items():
            own = _APP_ID_RE.search(body)
            suf = _SUFFIX_RE.search(body)
            flavors[name] = (own.group(1) if own else base, suf.group(1) if suf else "")
        dims = _DIMENSIONS_RE.search(android)
        if dims and len(re.findall(r"[\"'](\w+)[\"']", dims.group(1))) > 1:
            complete = False  # variants are the cross product of dimensions
    variants = []
    for bt, bt_suffix in build_types.items():
        if flavors:
            for fl, (fl_id, fl_suffix) in flavors.items():
                variants.append({"name": fl + _cap(bt), "application_id": fl_id + fl_suffix + bt_suffix})
        else:
            variants.append({"name": bt, "application_id": base + bt_suffix})
    module_dir = posixpath.dirname(path)
    module = ":" + module_dir.replace("/", ":") if module_dir else ""
    return {"file": path, "module": module, "application_id": base, "variants": variants,
            "complete": complete}


# ---------------------------------------------------------------------------
# Node: scripts and package manager
# ---------------------------------------------------------------------------

def parse_package_scripts(text: str | None) -> list[str] | None:
    import json

    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return sorted(scripts) if isinstance(scripts, dict) else []


def package_manager(tree: Iterable[str]) -> str | None:
    tree = set(tree)
    for lock, pm in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"),
                     ("package-lock.json", "npm")):
        if lock in tree:
            return pm
    return "npm" if "package.json" in tree else None


# ---------------------------------------------------------------------------
# Reading them (called from evidence_checks.gather_repo_evidence)
# ---------------------------------------------------------------------------

def gradle_build_files(tree: Iterable[str]) -> list[str]:
    """Application-module build files: those that apply the application plugin
    are found by reading; this lists the candidates, the app module first."""
    files = [p for p in tree if posixpath.basename(p) in ("build.gradle", "build.gradle.kts")
             and "/build/" not in p and p.count("/") <= 2]
    return sorted(files, key=lambda p: (not re.search(r"(^|/)app/build\.gradle", p), len(p)))[:6]


def read_command_facts(tree: list[str], texts: dict[str, str], platform: str | None,
                       bundle_ids: list[str] | None = None) -> dict:
    """What the repo's build config says commands and identifiers are.

    ``texts`` are the files read at the commit under test (gradle files,
    package.json). A file in the tree that is absent from ``texts`` could not
    be read, and the facts say so instead of being empty.
    """
    tree_set = set(tree)
    facts: dict[str, Any] = {"platform": platform, "unread": []}
    gradle = gradle_build_files(tree)
    if gradle:
        facts["gradle_wrapper"] = "gradlew" in tree_set
        apps = []
        for p in gradle:
            if p not in texts:
                facts["unread"].append(p)
                continue
            if re.search(r"com\.android\.application|android\.application|\bapplicationId\b", texts[p]):
                parsed = parse_android_variants(p, texts[p])
                if parsed:
                    apps.append(parsed)
        facts["android_apps"] = apps
    if "package.json" in tree_set:
        scripts = parse_package_scripts(texts.get("package.json")) if "package.json" in texts else None
        if scripts is None:
            facts["unread"].append("package.json")
        else:
            facts["package_scripts"] = scripts
        facts["package_manager"] = package_manager(tree)
    if bundle_ids:
        facts["bundle_ids"] = list(bundle_ids)
    return facts


def command_files(tree: list[str]) -> list[str]:
    return gradle_build_files(tree) + (["package.json"] if "package.json" in tree else [])


def render_command_facts(facts: dict | None) -> list[str]:
    """Lines for the prompt's BUILD/CI section, each naming its file."""
    if not facts:
        return []
    out = []
    if "gradle_wrapper" in facts:
        out.append("Gradle wrapper " + ("is committed: every Gradle command is `./gradlew …`, never `gradle …`"
                                        if facts["gradle_wrapper"] else "is NOT committed (no gradlew at the root)"))
    for app in facts.get("android_apps") or []:
        ids = "; ".join(f"{v['name']} → {v['application_id']}" for v in app["variants"])
        tasks = ", ".join(f"`./gradlew {app['module']}:test{_cap(v['name'])}UnitTest`"
                          for v in app["variants"][:4])
        out.append(f"Android app module {app['module'] or '(root)'} ({app['file']}): variant application "
                   f"ids {ids}{'' if app['complete'] else ' (may be incomplete: several flavour dimensions or a suffix set in code)'}. "
                   f"Use the id of the variant the tester installs (a debug build is usually the suffixed one). "
                   f"Unit tests run per variant: {tasks}")
    if facts.get("package_scripts") is not None:
        pm = facts.get("package_manager") or "npm"
        out.append(f"package.json scripts ({pm}): {', '.join(facts['package_scripts']) or '(none)'}; "
                   f"run them as `{pm} run <script>`. Any other script name does not exist")
    if facts.get("bundle_ids"):
        out.append(f"iOS bundle ids: {', '.join(facts['bundle_ids'])}")
    for p in facts.get("unread") or []:
        out.append(f"{p} could not be read, so commands from it are unverified")
    return out


# ---------------------------------------------------------------------------
# Rule T: the ticket's "before" commit
# ---------------------------------------------------------------------------

def first_pr(prs: list[dict]) -> dict | None:
    """The ticket's first PR in one repo: the lowest number with a base commit."""
    with_base = [p for p in prs if p.get("base_sha") and p.get("head_sha")]
    if not with_base:
        return None
    return min(with_base, key=lambda p: (p.get("number") is None, p.get("number") or 0))


async def ticket_base(github, repo: str, prs: list[dict]) -> dict | None:
    """``{"sha", "pr"}``: the merge base of the ticket's first PR in ``repo``.

    None when GitHub would not say — never the PR's base tip as a guess,
    because a branch rebased later has a different base, and "before" read
    from the wrong commit is exactly what this exists to stop.
    """
    pr = first_pr(prs)
    if pr is None or github is None:
        return None
    try:
        cmp = await github.compare_commits(repo, pr["base_sha"], pr["head_sha"])
    except Exception:
        return None
    sha = (cmp or {}).get("merge_base")
    return {"sha": sha, "pr": pr.get("number")} if sha else None


# ---------------------------------------------------------------------------
# Rule Y: re-read commands and identifiers in the plan
# ---------------------------------------------------------------------------

COMMAND_MARKER = "Unverified — command or identifier does not match the build config (rule Y):"

_BARE_GRADLE_RE = re.compile(r"(?<![\w./-])gradle\s+(?=[:\w-])")
_GRADLE_TASK_RE = re.compile(r"(?:\./gradlew|\bgradle)\s+((?::[\w-]+)*:?test(\w*?)UnitTest)\b")
_PKG_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_]+){2,}\b")
_ANDROID_CTX_RE = re.compile(r"\badb\b|\bpackage\b|\bapplicationId\b|\bdumpsys\b|\bpm\s+(?:clear|list)\b|"
                             r"\bam\s+start\b|/data/data/|\brun-as\b|\bmonkey\b", re.I)
_SCRIPT_RE = re.compile(r"\b(npm|pnpm|yarn|bun)\s+run\s+([\w:.-]+)")
_SIMCTL_RE = re.compile(r"\bsimctl\s+(?:launch|terminate|get_app_container|uninstall)\s+(?:booted\s+)?([\w.-]+)")


def _issues(text: str, facts_by_repo: list[dict]) -> list[str]:
    issues: list[str] = []
    wrapper = any(f.get("gradle_wrapper") for f in facts_by_repo)
    if wrapper and _BARE_GRADLE_RE.search(text):
        issues.append("uses `gradle`, but the repo commits a Gradle wrapper — use `./gradlew`")
    apps = [a for f in facts_by_repo for a in f.get("android_apps") or []]
    if apps:
        all_complete = all(a["complete"] for a in apps)
        ids = {v["application_id"] for a in apps for v in a["variants"]}
        names = {v["name"].lower() for a in apps for v in a["variants"]}
        for m in _GRADLE_TASK_RE.finditer(text):
            variant = m.group(2)
            if variant and variant.lower() not in names and all_complete:
                issues.append(f"task `{m.group(1)}` names variant '{variant}', which the build config does not "
                              f"define (variants: {', '.join(sorted(v['name'] for a in apps for v in a['variants']))})")
            elif not variant and all_complete:
                issues.append(f"`{m.group(1)}` runs no single variant; use `{apps[0]['module']}:test"
                              f"{_cap(apps[0]['variants'][0]['name'])}UnitTest` or another variant's task")
        if all_complete and _ANDROID_CTX_RE.search(text):
            bases = {a["application_id"] for a in apps}
            for tok in set(_PKG_TOKEN_RE.findall(text)):
                if tok in ids:
                    continue
                if any(tok == b or tok.startswith(b + ".") for b in bases):
                    issues.append(f"application id `{tok}` is not a variant's id "
                                  f"(variants: {', '.join(sorted(ids))})")
    scripts = [(f.get("package_manager"), set(f["package_scripts"])) for f in facts_by_repo
               if f.get("package_scripts") is not None]
    if scripts:
        known = set().union(*(s for _, s in scripts))
        for m in _SCRIPT_RE.finditer(text):
            if m.group(2) not in known:
                issues.append(f"`{m.group(1)} run {m.group(2)}`: package.json has no script "
                              f"'{m.group(2)}' (scripts: {', '.join(sorted(known))[:200]})")
    bundles = {b for f in facts_by_repo for b in f.get("bundle_ids") or []}
    if bundles:
        for m in _SIMCTL_RE.finditer(text):
            tok = m.group(1)
            if "." in tok and tok not in bundles and not tok.startswith("<"):
                issues.append(f"bundle id `{tok}` is not one the project builds ({', '.join(sorted(bundles))})")
    return list(dict.fromkeys(issues))


def check_commands(test_plan: Any, repo_evidence: list[dict] | None) -> list[dict]:
    """Mark every case step, expected result and regression line whose command or
    identifier the build config contradicts. Marks in place; returns findings."""
    facts = [e.get("commands") for e in repo_evidence or [] if isinstance(e, dict) and e.get("commands")]
    if not facts:
        return []
    findings = []
    for section in ("happy_path", "edge_cases", "integration_tests", "security_negative_tests"):
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if not isinstance(case, dict):
                continue
            steps = case.get("steps") or []
            for n, step in enumerate(steps):
                if not isinstance(step, str) or COMMAND_MARKER in step:
                    continue
                issues = _issues(step, facts)
                if issues:
                    steps[n] = f"{COMMAND_MARKER} {'; '.join(issues)} — {step}"
                    findings.append({"case_id": f"{section}:{i}", "where": f"step {n + 1}",
                                     "rule": "Y", "reason": "; ".join(issues)})
            for key in ("expected", "preconditions", "how_to_verify"):
                text = case.get(key)
                if isinstance(text, str) and COMMAND_MARKER not in text:
                    issues = _issues(text, facts)
                    if issues:
                        case[key] = f"{COMMAND_MARKER} {'; '.join(issues)} — {text}"
                        findings.append({"case_id": f"{section}:{i}", "where": key, "rule": "Y",
                                         "reason": "; ".join(issues)})
    lines = getattr(test_plan, "regression_checklist", None) or []
    for i, line in enumerate(lines):
        if isinstance(line, str) and COMMAND_MARKER not in line:
            issues = _issues(line, facts)
            if issues:
                m = re.match(r"^(\s*(?:🔴|🟡|🟢)\s*)", line)
                head = m.group(1) if m else ""
                lines[i] = f"{head}{COMMAND_MARKER} {'; '.join(issues)} — {line[len(head):]}"
                findings.append({"case_id": f"regression_checklist:{i}", "where": "line", "rule": "Y",
                                 "reason": "; ".join(issues)})
    return findings
