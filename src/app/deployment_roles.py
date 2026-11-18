"""Which deployment of an app serves the surface a case tests (rule AJ).

One app often runs as several deployments, sometimes recorded in different
deployments repos and running in different clusters: a public copy that
serves the API and the web host, and an internal copy that runs the cron
scheduler or a queue consumer and serves no request. Until this module the
plan picked one by environment name. On SK-3060 (plan 721) the build check
and the boot-log step both pointed at the internal Agenda worker
(``RUN_AGENDA_CRONS: "true"``, no host), while every case called tRPC routes
on the public host served by the copy in a second deployments repo. Loki for
the worker showed only health checks. A pass there proves nothing about the
surface under test, and a mismatch there can block a correct build.

Every values file read for a build check is classified here:

``serving``
    It routes a host to the app (an ingress, an Envoy/Istio host, a gateway
    route): ``hosts`` lists them, each with the line it came from.
``internal``
    No host, and a worker/cron signal: ``RUN_*_CRONS: "true"``, ``*_WORKER:
    "true"``, a CronJob schedule, an ingress turned off, or the file calling
    itself a worker.
``unknown``
    Neither. Chart defaults may add the host, so this is never read as
    internal.

An environment variable holding the app's public URL (``EXPO_PUBLIC_BASE_URL``,
``*_BASE_URL``) is not a host signal: on SK-3060 the worker set it to the
public host too, for links in the emails it sends.

The build check (``build_targets._container_check``) and log-reading steps
(``check_log_steps``) then name the serving copy, list internal copies
separately, and say "confirm which deployment serves <host>" when nothing
shows which one does. ``verify_build_check`` is the re-check before save: a
build-check line that names a deployment without a host while another
candidate routes one is downgraded where it stands.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

ROLES = ("serving", "internal", "unknown")
RULE = "AJ"

_HOSTNAME = r"(?:\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+\.[a-z]{2,}"
#: ``host: a.example.com`` / ``- host: …`` / ``hostname: …`` (lowercase keys:
#: an env var such as ``FILES_DB_HOST`` is configuration, not a route).
_HOST_KEY_RE = re.compile(rf"^\s*(?:-\s+)?(?:host|hostname|hosts|domain)\s*:\s*[\"']?({_HOSTNAME})[\"']?\s*(?:#.*)?$")
#: ``hosts: [a.example.com, b.example.com]``
_HOST_LIST_RE = re.compile(r"^\s*(?:-\s+)?hosts\s*:\s*\[([^\]]*)\]")
_HOSTS_BLOCK_RE = re.compile(r"^(\s*)(?:-\s+)?hosts\s*:\s*(?:#.*)?$")
_LIST_ITEM_RE = re.compile(rf"^\s*-\s+[\"']?({_HOSTNAME})[\"']?\s*(?:#.*)?$")

_TRUE = r"[\"']?(?:true|yes|on|1)[\"']?"
_INTERNAL_FLAGS = (
    re.compile(rf"^\s*(?:-\s+)?(RUN_\w*(?:CRON|JOB|WORKER|SCHEDULER|QUEUE|CONSUMER)\w*)\s*:\s*{_TRUE}\s*(?:#.*)?$", re.I),
    # ``RUN_WORKER: "true"``, ``IS_CONSUMER: true``, ``WORKER_ENABLED: "yes"`` — the
    # flag itself, not a worker setting (``services__worker__maxCommands: "1"``).
    re.compile(r"^\s*(?:-\s+)?((?:(?:RUN|IS|ENABLE|ENABLED|START)_?)?(?:[A-Z]+_)?(?:WORKER|CONSUMER|SCHEDULER)"
               r"(?:_?(?:ENABLED|MODE|ONLY))?)\s*:\s*[\"']?(?:true|yes|on)[\"']?\s*(?:#.*)?$", re.I),
    re.compile(r"^\s*(?:-\s+)?((?:PROCESS_TYPE|APP_ROLE|ROLE|MODE|SERVICE_MODE|RUN_MODE)\s*:\s*[\"']?"
               r"(?:worker|cron|consumer|scheduler|jobs?)[\"']?)\s*$", re.I),
    re.compile(r"^\s*(kind\s*:\s*CronJob)\b"),
    re.compile(r"^\s*(?:-\s+)?(schedule\s*:\s*[\"'][0-9*/,\- ]+\s[0-9*/,\- ]+[\"'])"),
)
#: The file describing itself: "This worker owns every Agenda job".
_SELF_WORKER_RE = re.compile(r"#.*\bthis\s+(?:worker|cron(?:job)?|queue\s+consumer|scheduler)\b", re.I)
_INGRESS_RE = re.compile(r"^(\s*)(?:ingress|envoy-host|virtualService|httpRoute|gateway)\s*:\s*$", re.I)
_ENABLED_FALSE_RE = re.compile(r"^\s*enabled\s*:\s*[\"']?false[\"']?")


def classify_values(text: str | None) -> dict:
    """``{"role", "hosts", "signals"}`` for one deployment values file.

    ``signals`` are ``"line N: <text>"`` strings, so a plan can cite the file
    line that decided the role. A host wins over a worker flag: a copy that
    serves traffic and also runs crons serves the cases.
    """
    hosts: list[str] = []
    host_lines: list[str] = []
    internal: list[str] = []
    lines = (text or "").splitlines()
    block_indent: int | None = None
    ingress_indent: int | None = None
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if ingress_indent is not None and indent <= ingress_indent and not stripped.startswith("#"):
            ingress_indent = None
        if block_indent is not None and not stripped.startswith("#"):
            item = _LIST_ITEM_RE.match(line)
            if item and indent >= block_indent:
                _add(hosts, host_lines, item.group(1), n, stripped)
                continue
            if indent <= block_indent and not stripped.startswith("-"):
                block_indent = None
        m = _INGRESS_RE.match(line)
        if m:
            ingress_indent = len(m.group(1))
            continue
        if ingress_indent is not None and _ENABLED_FALSE_RE.match(line) and indent > ingress_indent:
            internal.append(f"line {n}: ingress disabled ({stripped})")
            continue
        if stripped.startswith("#"):
            if _SELF_WORKER_RE.search(stripped):
                internal.append(f"line {n}: {stripped[:120]}")
            continue
        m = _HOST_KEY_RE.match(line)
        if m:
            _add(hosts, host_lines, m.group(1), n, stripped)
            continue
        m = _HOST_LIST_RE.match(line)
        if m:
            for h in re.findall(_HOSTNAME, m.group(1)):
                _add(hosts, host_lines, h, n, stripped)
            continue
        m = _HOSTS_BLOCK_RE.match(line)
        if m:
            block_indent = len(m.group(1))
            continue
        for rx in _INTERNAL_FLAGS:
            fm = rx.match(line)
            if fm:
                internal.append(f"line {n}: {stripped[:120]}")
                break
    if hosts:
        return {"role": "serving", "hosts": hosts, "signals": host_lines}
    # A comment alone ("this worker …") is enough only beside no host at all;
    # flags and disabled ingress are the stronger signals and come first.
    if internal:
        internal.sort(key=lambda s: "#" in s)
        return {"role": "internal", "hosts": [], "signals": internal[:4]}
    return {"role": "unknown", "hosts": [], "signals": []}


def _add(hosts: list[str], lines: list[str], host: str, n: int, text: str) -> None:
    host = host.lower()
    if host not in hosts:
        hosts.append(host)
        lines.append(f"line {n}: {text[:120]}")


# ---------------------------------------------------------------------------
# What a case calls
# ---------------------------------------------------------------------------

_URL_HOST_RE = re.compile(rf"https?://({_HOSTNAME})", re.I)
#: A hostname written without a scheme needs a real-looking TLD, or
#: ``profile.result.id`` and ``req.body.templateIds`` read as hosts.
_BARE_HOST_RE = re.compile(
    r"(?<![\w@./-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){2,}(?:com|net|org|io|dev|app|co|us|ai|cloud|tech|"
    r"site|info|biz|pe|me|gov|edu|internal|local))(?![\w.-])", re.I)
_NOT_HOSTS = re.compile(r"\.(?:ya?ml|json|tsx?|jsx?|py|go|cs|swift|kt|java|rb|md|txt|log|html|css|lock|"
                        r"plist|gradle|xml|env|sh|cfg|ini|toml)$", re.I)
#: Hosts of the tools a step uses to look at the app, not the app itself.
_TOOL_HOST_RE = re.compile(
    r"(?:^|\.)(?:github\.com|githubusercontent\.com|atlassian\.net|atlassian\.com|figma\.com|slack\.com|"
    r"google\.com|googleapis\.com|grafana\.net|grafana\.com|launchdarkly\.com|datadoghq\.com|"
    r"loom\.com|okta\.com|oktapreview\.com|amazonaws\.com|apple\.com|crontab\.guru|example\.org|"
    r"posthog\.com|sentry\.io|segment\.io|mixpanel\.com|heapanalytics\.com|hubspot\.com)$"
    r"|^(?:grafana|loki|argocd?|kibana|sentry|jenkins|ci|vault)\.", re.I)
_JOB_RE = re.compile(
    r"\b(?:cron(?:job)?s?|agenda|scheduler|scheduled\s+(?:job|task|run)s?|background\s+jobs?|"
    r"queue\s+consumers?|job\s+runs?|worker\s+(?:pod|deployment|process|logs?)|"
    r"RUN_\w*CRONS?)\b",
    re.I,
)


def case_text(case: dict) -> str:
    parts = [str(case.get(k) or "") for k in ("title", "preconditions", "environment", "trigger",
                                               "expected", "test_data")]
    parts += [s for s in case.get("steps") or [] if isinstance(s, str)]
    return " ".join(parts)


def hosts_in(text: str) -> list[str]:
    """Hostnames a case calls: URL hosts first, then bare hostnames."""
    out: list[str] = []
    for rx in (_URL_HOST_RE, _BARE_HOST_RE):
        for m in rx.finditer(text or ""):
            h = m.group(1).lower().rstrip(".")
            if _NOT_HOSTS.search(h) or _TOOL_HOST_RE.search(h) or h in out:
                continue
            out.append(h)
    return out


def app_hosts(hosts: Iterable[str], repo: str) -> list[str]:
    """The hosts named for the app (``agent-calculator-staging.…`` for
    ``agent-calculator``), else all of them: a case also calls Okta, analytics
    and other services, which are not the question."""
    hosts = list(hosts)
    words = [w for w in re.split(r"[-_.]", (repo or "").rsplit("/", 1)[-1].lower()) if len(w) >= 4]
    named = [h for h in hosts if words and all(w in h for w in words)]
    return named or hosts


def is_job_case(case: dict) -> bool:
    """The case exercises cron / scheduled job / queue behaviour."""
    return bool(_JOB_RE.search(case_text(case)))


def host_matches(host: str, served: Iterable[str]) -> bool:
    for s in served:
        if s == host or (s.startswith("*.") and host.endswith(s[1:])):
            return True
    return False


# ---------------------------------------------------------------------------
# Choosing the deployment a check names
# ---------------------------------------------------------------------------

def partition(deploy: list[dict], hosts: Iterable[str] = ()) -> dict:
    """Split one repo's deploy entries by role.

    ``{"serving", "internal", "unknown", "matched", "classified"}``:
    ``matched`` are the serving entries whose hosts include a host the cases
    call (empty when the cases name none or none matches). ``classified`` is
    False when no entry carries a role (older stored entries), so callers keep
    their old behaviour.
    """
    hosts = list(hosts)
    out = {"serving": [], "internal": [], "unknown": [], "matched": [],
           "classified": any(d.get("role") for d in deploy)}
    for d in deploy:
        out[d.get("role") if d.get("role") in ROLES else "unknown"].append(d)
    if hosts:
        out["matched"] = [d for d in out["serving"]
                          if any(host_matches(h, d.get("hosts") or []) for h in hosts)]
    return out


def describe(d: dict) -> str:
    """``staging-east (owner/repo/path)``."""
    src = d.get("source") or ""
    return f"{d.get('env')} ({src})" if src else str(d.get("env"))


def why_internal(d: dict) -> str:
    """``internal, no host (line 47: RUN_AGENDA_CRONS: "true")`` — the strongest signal."""
    sig = (d.get("signals") or [""])[0]
    return f"internal, no host ({sig})" if sig else "internal, no host"


def host_phrase(hosts: Iterable[str]) -> str:
    hosts = list(hosts)
    return " / ".join(hosts[:3]) if hosts else "the host these cases call"


def internal_note(internal: list[dict], jobs: bool) -> str:
    """The sentence that lists internal copies apart from the check."""
    if not internal:
        return ""
    names = "; ".join(f"{describe(d)}" + (f" runs {d['tag']}" if d.get("tag") else "")
                      + f", {why_internal(d)}" for d in internal)
    if jobs:
        return (f" Internal copies (no host — these serve no request): {names}. Check them too, "
                f"with the same compare, only for the cases that test cron / scheduled-job "
                f"behaviour, since that runs there.")
    return (f" Not this check: {names}. No request from these cases reaches an internal copy, so "
            f"its image tag, pods and logs prove nothing about them.")


# ---------------------------------------------------------------------------
# Rule AJ, part 3: log-reading steps
# ---------------------------------------------------------------------------

_LOG_STEP_RE = re.compile(
    r"\b(?:logs?|loki|logql|kubectl\s+logs|stdout|startup\s+line|boot\s+line|log\s+line)\b", re.I)
_LOG_WHERE_RE = re.compile(r"\b(?:pods?|deployment|workload|namespace|cluster|server|container|"
                           r"loki|grafana|kubectl|argo\s?cd|service)\b", re.I)
_TIERS = (("prod", r"prod\w*|production"), ("staging", r"staging|stage|stg"),
          ("integ", r"integ\w*|integration"), ("dev", r"dev|development"), ("qa", r"qa|uat|test"))


def tier(env: str | None) -> str | None:
    """``staging`` for ``staging-east`` and ``staging.staging-west``."""
    parts = [x for x in re.split(r"[-_.]", (env or "").lower()) if x]
    for name, rx in _TIERS:
        if any(re.fullmatch(rx, x) for x in parts):
            return name
    return None


_ENV_TOKEN_RE = re.compile(r"(?:^|[-_.])(?:integ\w*|staging|stage|stg|prod\w*|dev|qa|uat|test|sandbox|"
                           r"preview|demo|east|west|ops)(?:$|[-_.])", re.I)
LOG_MARKER = "Wrong deployment for these logs (rule AJ): {why} —"
LOG_CONFIRM_MARKER = "Confirm the deployment first (rule AJ): {why} —"
_MARKED_RE = re.compile(r"\brule AJ\b")


def _env_named(text: str, d: dict) -> bool:
    env = str(d.get("env") or "")
    src = str(d.get("source") or "")
    if src and src in text:
        return True
    if not env:
        return False
    # ``staging.staging-west`` is also written ``staging-west``. A bare name
    # counts only when it reads as an environment: a directory named for the
    # service itself (``offer-management``) would match every mention of it.
    names = {env} | ({env.split(".", 1)[1]} if "." in env else set())
    return any(re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", text, re.I)
               for n in names if len(n) >= 4 and _ENV_TOKEN_RE.search(n))


def _repo_entries(targets: list[dict]) -> list[tuple[dict, list[dict]]]:
    return [(t, t.get("deploy") or []) for t in targets
            if t.get("platform") not in ("ios", "android") and len(t.get("deploy") or []) >= 1]


def _serving_sentence(serving: list[dict], hosts: list[str]) -> str:
    return (f"requests to {host_phrase(hosts)} are served by "
            + " or ".join(describe(d) for d in serving)
            + " — read that deployment's pod logs")


def check_log_steps(test_plan: Any, targets: list[dict], report: dict | None = None) -> list[dict]:
    """Mark, in place, log-reading steps aimed at a copy the case never reaches.

    A step that names an internal copy (by environment or values path) in a
    case that is not about cron/job behaviour is prefixed with the serving
    copy to read instead, or with "confirm which deployment serves <host>"
    when none is known. A step that names no copy, on an app with an internal
    copy beside it, gets the serving copy appended. Step numbers never move.
    """
    from .build_targets import targets_for_case
    from .evidence_checks import iter_cases

    out: list[dict] = []
    with_roles = [t for t, d in _repo_entries(targets) if any(e.get("role") for e in d)]
    if not with_roles:
        return out
    for section, i, case in iter_cases(test_plan):
        if not isinstance(case, dict):
            continue
        mine = [t for t in targets_for_case(case, with_roles)] if len(with_roles) > 1 else with_roles
        if not mine:
            continue
        all_hosts = hosts_in(case_text(case))
        jobs = is_job_case(case)
        steps = case.get("steps") or []
        for n, step in enumerate(steps):
            if not isinstance(step, str) or _MARKED_RE.search(step):
                continue
            if not (_LOG_STEP_RE.search(step) and _LOG_WHERE_RE.search(step)):
                continue
            for t in mine:
                hosts = app_hosts(all_hosts, t["repo"])
                p = partition(t["deploy"], all_hosts)
                serving = p["matched"] or p["serving"]
                named_internal = [d for d in p["internal"] if _env_named(step, d)]
                named_serving = [d for d in serving if _env_named(step, d)]
                if named_internal and not jobs and not named_serving:
                    why = "; ".join(f"{describe(d)} is {why_internal(d)}" for d in named_internal)
                    # The step meant this environment: the staging worker's
                    # public counterpart is the staging copy, not all of them.
                    tiers = {tier(d.get("env")) for d in named_internal} - {None}
                    if not p["matched"] and tiers:
                        serving = [d for d in serving if tier(d.get("env")) in tiers] or serving
                    if serving:
                        marker = LOG_MARKER.format(why=f"{why}; {_serving_sentence(serving, hosts)}")
                    else:
                        cands = [d for d in t["deploy"] if d not in named_internal]
                        others = (f"; other candidates: {', '.join(_tagged(d) for d in cands)}"
                                  if cands else "; no deployment read for this app routes a host")
                        marker = LOG_CONFIRM_MARKER.format(
                            why=f"{why}{others}; confirm which deployment serves {host_phrase(hosts)} "
                                f"and read its pod logs")
                    steps[n] = f"{marker} {step}"
                    out.append(_finding(section, i, case, n, "marked", marker))
                    break
                if named_internal or named_serving:
                    continue
                # No copy named: say which one when the app runs as more than
                # one kind of copy (an internal one beside it, or copies in
                # several deployments repos). Plain integ / staging / prod
                # copies of one service are the case's environment's call.
                if not (p["internal"] or len({_deploy_repo(d) for d in t["deploy"]}) > 1):
                    continue
                if jobs and p["internal"]:
                    hint = (f"cron / job behaviour runs on {' or '.join(describe(d) for d in p['internal'])}; "
                            f"read that copy's logs for it")
                elif serving:
                    hint = (f"read the logs of {' or '.join(describe(d) for d in serving)}, the deployment "
                            f"that serves {host_phrase(hosts)}"
                            + (f"; {', '.join(describe(d) for d in p['internal'])} is internal and no "
                               f"request from this case reaches it" if p["internal"] else ""))
                else:
                    hint = (f"several deployments run {t['repo']}: "
                            f"{', '.join(_tagged(d) for d in t['deploy'])}; confirm which one serves "
                            f"{host_phrase(hosts)} before reading its logs")
                steps[n] = f"{step.rstrip()} (Which deployment, rule AJ: {hint}.)"
                out.append(_finding(section, i, case, n, "fixed", hint))
                break
    if report is not None:
        for f in out:
            report.setdefault("findings", []).append(
                {"section": f["section"], "title": f["title"], "rule": RULE, "action": f["action"],
                 "reason": f["reason"]})
    return out


def _deploy_repo(d: dict) -> str:
    return "/".join((d.get("source") or "").split("/")[:2])


def _tagged(d: dict) -> str:
    tag = f" runs {d['tag']}" if d.get("tag") else ""
    role = {"serving": "routes " + ", ".join(d.get("hosts") or []) if d.get("hosts") else "serving",
            "internal": "internal, no host", "unknown": "no host in its values file"}.get(d.get("role") or "unknown")
    return f"{describe(d)}{tag} ({role})"


def _finding(section: str, i: int, case: dict, n: int, action: str, reason: str) -> dict:
    if action == "marked":
        case.setdefault("grounding_downgrades", []).append(
            {"claim": f"rule {RULE}: step {n + 1} reads logs on a deployment the case never reaches"[:200],
             "reason": reason})
    return {"case_id": f"{section}:{i}", "section": section, "title": (case.get("title") or "")[:140],
            "step": n + 1, "action": action, "reason": reason[:400]}


# ---------------------------------------------------------------------------
# Rule AJ, part 5: re-check the build-check lines before save
# ---------------------------------------------------------------------------

BUILD_MARKER = "Build check names the wrong deployment (rule AJ): {why} —"
_BUILD_LINE_RE = re.compile(r"\b(?:image\s+tag|values\.ya?ml|build\s+check|deploy(?:ment)?\s+check|"
                            r"runs\s+(?:main|master|release)[\w.-]*-[0-9a-f]{7}|deployed\s+(?:image|commit))",
                            re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", text or "") if s.strip()]


def verify_build_check(test_plan: Any, top: dict | None, targets: list[dict],
                       report: dict | None = None) -> dict:
    """Confirm each build-check line names a deployment that serves the cases.

    The top-of-plan check (``top["build_check"]``) and any case line that
    still reads a build (a precondition such as "staging-east runs main-…")
    are checked: a line that names an internal copy, or a copy with no host
    while another candidate routes one, is downgraded where it stands. A
    named serving copy is confirmed only when its values file routes a host
    the cases call (or the cases name no host). Returns the provenance entry.
    """
    from .evidence_checks import iter_cases

    result = {"checked": 0, "confirmed": [], "downgraded": [], "unconfirmed": []}
    entries = [(t, t.get("deploy") or []) for t in targets or []
               if t.get("platform") not in ("ios", "android") and any(d.get("role") for d in t.get("deploy") or [])]
    if not entries:
        return result
    all_hosts: list[str] = []
    for _, _, case in iter_cases(test_plan):
        if isinstance(case, dict):
            for h in hosts_in(case_text(case)):
                if h not in all_hosts:
                    all_hosts.append(h)

    def judge(text: str, jobs: bool) -> str | None:
        for t, deploy in entries:
            p = partition(deploy, all_hosts)
            hosted = p["serving"]
            for d in deploy:
                if not _env_named(text, d) or _ruled_out(text, d):
                    continue
                if d.get("role") == "serving":
                    routed = ", ".join(d.get("hosts") or [])
                    hit = [h for h in all_hosts if host_matches(h, d.get("hosts") or [])]
                    if hit:
                        result["confirmed"].append(f"{describe(d)} routes {', '.join(hit)}")
                    elif all_hosts:
                        result["unconfirmed"].append(
                            f"{describe(d)} routes {routed}, not {host_phrase(all_hosts)}")
                    else:
                        result["unconfirmed"].append(f"{describe(d)} routes {routed}; the cases name no host")
                    continue
                if jobs and d.get("role") == "internal":
                    continue
                if hosted:
                    what = (f"is {why_internal(d)}" if d.get("role") == "internal"
                            else "routes no host in its values file")
                    return (f"{describe(d)} {what}, "
                            f"while {' or '.join(describe(s) for s in (p['matched'] or hosted))} routes "
                            f"{', '.join((p['matched'] or hosted)[0].get('hosts') or [])}; check that one")
                if d.get("role") == "internal":
                    return (f"{describe(d)} is {why_internal(d)}; confirm which deployment serves "
                            f"{host_phrase(all_hosts)} and read its image tag")
        return None

    check = (top or {}).get("build_check") if isinstance(top, dict) else None
    if isinstance(check, dict) and isinstance(check.get("step"), str):
        result["checked"] += 1
        # The code-written check names internal copies only in its "Not this
        # check" / "Internal copies" sentences; judge the rest.
        primary = re.split(r"\s(?:Not this check|Internal copies \(no host|Also found, with no host)",
                           check["step"])[0]
        why = judge(primary, jobs=False)
        if why:
            check["step"] = f"{BUILD_MARKER.format(why=why)} {check['step']}"
            check["targeting"] = {"status": "downgraded", "reason": why}
            result["downgraded"].append({"where": "build_check", "reason": why})
    for section, i, case in iter_cases(test_plan):
        if not isinstance(case, dict):
            continue
        jobs = is_job_case(case)
        for key in ("preconditions", "environment"):
            text = case.get(key)
            if not isinstance(text, str) or _MARKED_RE.search(text):
                continue
            for sent in _sentences(text):
                if not _BUILD_LINE_RE.search(sent):
                    continue
                result["checked"] += 1
                why = judge(sent, jobs)
                if why:
                    case[key] = text.replace(sent, f"{BUILD_MARKER.format(why=why)} {sent}", 1)
                    case.setdefault("grounding_downgrades", []).append(
                        {"claim": f"rule {RULE}: build check on a deployment the case never reaches",
                         "reason": why})
                    result["downgraded"].append({"where": f"{section}:{i} {key}", "reason": why,
                                                 "title": (case.get("title") or "")[:140]})
                    break
    if report is not None:
        for d in result["downgraded"]:
            report.setdefault("findings", []).append(
                {"section": d["where"].split(":")[0], "title": d.get("title") or "Build check",
                 "rule": RULE, "action": "downgraded", "reason": d["reason"]})
    result["confirmed"] = sorted(set(result["confirmed"]))
    result["unconfirmed"] = sorted(set(result["unconfirmed"]))
    return result


def _ruled_out(text: str, d: dict) -> bool:
    """The line names the copy only to set it aside ("not staging-east")."""
    env = re.escape(str(d.get("env") or ""))
    return bool(env and re.search(rf"\b(?:not|never|instead of|rather than|except)\s+(?:the\s+)?{env}\b",
                                  text, re.I))
