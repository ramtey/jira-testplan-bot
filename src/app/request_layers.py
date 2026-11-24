"""A request-level case states who it runs as, and expects the answer of the layer that answers first (rule AM).

SK-3174 (plan 752 v1) shipped three regression one-liners a tester could not run as written:

* "Batch requests: POST /mcp with a JSON array of two tools/call. Expected: 400
  JSON-RPC -32600". No auth. The bearer check runs in the server's onRequest
  hook and the batch check runs in the handler after it, so the request as
  written got the auth hook's 401 and never reached the code at risk.
* "Wrong host: POST /mcp with Host: evil.example. Expected: 421". Through the
  public hostname Cloudflare answered 403 before envoy or the app saw it. The
  plan's topology named only envoy, and the 421 is asserted only by a unit
  test (server/test/app.test.ts › "answers a wrong Host with the same 421 as
  before").
* "Okta key outage … Only if the platform team can block JWKS". The tester
  cannot cause the outage; a unit test asserts the 503.

Security-negative cases carry a structured ``request.auth``; regression lines
are bare strings, and nothing checked the order of the layers a request passes.
This module runs before save, over every section (regression lines included):

1. **Auth stated.** A case that sends an HTTP request to a route the plan or
   the code shows is authenticated, whose subject is not authentication itself
   and which names no principal, gets "Runs as: … Auth: …" — from the plan's
   own credentials for that route when it has them — and the reason: the auth
   layer answers first without it.
2. **Auth answers first.** A case that deliberately sends no credentials to an
   authenticated route but expects a later check's status is marked: the auth
   layer's answer is what a tester sees.
3. **Edge answers first.** Host-header and header-spoofing cases, when the
   topology shows a CDN/WAF or cannot rule one out, expect "refused, record
   which layer answered" from outside; the app's own status is pointed at the
   unit test that asserts it, or marked not externally verifiable.
4. **Infrastructure triggers.** A case whose only trigger is an outage or
   action a tester cannot take names the test that asserts it, or is marked
   manual with the owning team.
5. **Order unknown.** A request with no auth stated, to a route the code read
   does not show to be public or authenticated, that pins an error status, is
   held to the outcome and the exact status is marked unverified.

Unread source never flags as absent: a route is "authenticated" only on
evidence (a no-token security case answered 401/403, an auth hook or guard in
code that was read), "public" only on evidence, and otherwise unknown.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

from .deployed_env_checks import ALL_CASE_SECTIONS, _calls, _text, route_matches
from .plan_recheck import _record

RULE = "AM"
AUTH_NOTE = ("Runs as: {principal}. Auth: {auth}. (rule AM: {route} needs credentials — {where}; without them it "
             "answers {status} before the check this case is about runs.)")
AUTH_FIRST_MARKER = ("Answered first by the auth layer (rule AM): with no credentials the request gets {status} "
                     "({where}) before the check this Expected names runs; expect that, or send a valid token to "
                     "reach the check —")
EDGE_EXPECTED = ("From outside, {edge} may answer first (rule AM): it sits in front of {behind} and can refuse this "
                 "request itself (for example a 403 page) before the app's check runs. Pass if the request is refused "
                 "and nothing reaches the app; record the status and the server, via and cf-ray headers to see which "
                 "layer answered. The app's own answer ({original}) is {proof}.")
INFRA_TEST_NOTE = ("Covered by {test} (rule AM: a tester cannot cause this from outside; read that test's result in "
                   "CI, or run it).")
INFRA_TEST_AND_OWNER_NOTE = ("Asserted by {test}; running this case itself is manual and needs {owner} (rule AM: a "
                             "tester cannot cause this from outside).")
INFRA_MANUAL_NOTE = ("Manual — needs {owner} (rule AM): a tester cannot cause this from outside and no test asserting "
                     "it was found; run only with {owner}, otherwise skip.")
ORDER_MARKER = ("Order of checks unverified (rule AM): no principal is stated and the code read does not show which "
                "checks run before this one on {route}; pass on the outcome (refused, no downstream call) and record "
                "the exact status —")

# --- what a case says ---------------------------------------------------------

_AUTH_STATED_RE = re.compile(
    r"\bRuns as:|\bAuth:|\bAuthorization\b|\bBearer\b|\b(?:access|id|api|session|auth|jwt|valid)[ _-]?tokens?\b|"
    r"\btoken for\b|\bas (?:the |an? |a |test |another |second )?(?:[\w-]+ )?(?:user|member|admin|owner|tc|agent|"
    r"broker|delegate|tester|principal|account|caller|recipient|sender|signer)s?\b|"
    r"\b[Aa]s [A-Z]\w* [A-Z]\b|\b[Aa]s [A-Z]\b(?!\w)|\blogged[- ]in\b|\btokens?\b|\bauthenticated (?:user|caller)\b|"
    r"\b(?:called|sent|made|requested) (?:by|as) |\b(?:owner|admin|delegate|integrator|signer|sender|recipient)(?:'s)?\b|"
    r"\bsigned[- ]in\b|\bsession cookie\b|\bcookie\b|"
    r"\bAPI key\b|\bx-api-key\b|\bclient[_ ]credentials\b", re.I)
_AUTH_SUBJECT_RE = re.compile(
    r"\bno (?:Authorization|auth\w*|token|bearer|credentials?)\b|\bwithout (?:an? |any )?(?:token|Authorization|auth\w*|"
    r"credentials?|bearer)\b|\bunauthenticated\b|\banonymous\b|\b(?:invalid|expired|malformed|revoked|forged|garbage|"
    r"missing|wrong)[ -](?:bearer |access |id )?(?:token|Authorization|signature|credentials?)\b|\bWWW-Authenticate\b|"
    r"\b401\b|\bunauthori[sz]ed\b|\bsign[- ]?in\b|\blog[- ]?in\b|\bdeleted user\b", re.I)
_EDGE_INPUT_RE = re.compile(
    # "Host:" as a header the case sends, not "Host: <the hostname under test>" as a label.
    r"(?:-H\s*['\"]?|\bheaders?\s*:?\s*\{?\s*['\"]?|\bwith\s+(?:an?\s+|the\s+)?(?:header\s+)?)Host['\"]?:\s*\S|"
    r"\bHost header\b|\b(?:wrong|spoofed|forged|bad|unknown|other|foreign|mismatched|evil)[ -]host\b|"
    r"\bX-Forwarded-(?:For|Host|Proto)\b|\bX-Real-IP\b|\bCF-Connecting-IP\b|\bX-Original-(?:URL|Host)\b|"
    r"\bForwarded:\s*(?:for|by|proto|host)=|\bTrue-Client-IP\b|\bheader spoof\w*|\bspoof\w* (?:the )?\w*[ -]?header\b", re.I)
_EDGE_TERM_RE = re.compile(
    r"\b(?:Cloudflare|CDN|WAF|Akamai|Fastly|CloudFront|Front Door|Imperva|edge(?: layer| proxy| network)?|"
    r"server: ?cloudflare|cf-ray)\b", re.I)
_NO_EDGE_RE = re.compile(r"\bno (?:CDN|WAF|edge)\b|\bnot proxied\b|\bdirect(?:ly)? (?:to|from) (?:the )?(?:pod|service|"
                         r"origin|load balancer)\b|\bDNS only\b", re.I)
# A case that sends the request to the origin itself (port-forward, in-cluster) has already
# changed its input to pass the edge (rule AM, point 2); "from outside" does not apply.
ORIGIN_DIRECT_RE = re.compile(r"\bport[- ]forward\w*|\blocalhost\b|\b127\.0\.0\.1\b|\bin[- ]cluster\b|\bkubectl\b|"
                              r"\bdirectly (?:to|against|at) the (?:pod|app|service|origin)\b|\bat the origin\b", re.I)
_PROXY_RE = re.compile(r"\b(envoy|nginx|ingress|ALB|ELB|load balancer|traefik|haproxy|api gateway|gateway|istio)\b",
                       re.I)
# Server-side infrastructure a tester cannot switch off. The tester's own
# network, device or browser (airplane mode, devtools offline, killing the
# client) is not on this list: those they can do.
_INFRA_NOUN = (r"(?:JWKS|key ?set|keys? endpoint|signing keys|IdP|identity provider|Okta|Auth0|Cognito|database|DB|"
               r"Mongo(?:DB)?|Postgres|Redis|queue|SQS|Kafka|dependency|upstream (?:service|API)|vendor (?:API|service)|"
               r"token[- ]exchange(?: service)?)")
_INFRA_RE = re.compile(
    r"\bonly if\b[^.;]{0,100}\b(?<!test )(?<!QA )(?:team|SRE|on-?call)\b|"
    r"\b(?:block|take down|bring down|shut down|stop|disable|break|cut off|revoke access to)\s+(?:the\s+)?"
    r"(?:[\w-]+\s+){0,2}" + _INFRA_NOUN + r"\b"
    # "stop at the Okta consent screen", "break the Okta session": the tester's own UI, not the service.
    r"(?!\s+(?:consent|screen|page|sign|login|log-in|prompt|session|form|button|tab|window))|"
    r"\b(?:simulate|cause|force|induce)\s+(?:an?\s+)?(?:[\w-]+\s+){0,2}(?:outage|downtime)\b|"
    r"\bwhile\s+(?:the\s+)?(?:[\w-]+\s+){0,2}?" + _INFRA_NOUN + r"\s+is\s+(?:down|unreachable|unavailable)\b|"
    # "Engineering deploys a pod with MCP_ISSUER pointed at a blackhole": another team acts on the infrastructure.
    # A deployment someone else must reconfigure for this case ("scratch pods with MCP_ISSUER overridden").
    r"\b(?:reconfigured|scratch|overridden|re-?pointed)\b[^.;]{0,60}\b(?:pods?|deployments?|instances?)\b|"
    r"\b(?:pods?|deployments?|instances?)\b[^.;]{0,30}\bwith\s+[A-Z][A-Z0-9_]{3,}\s+(?:overridden|set to|pointed)\b|"
    r"\b(?:Engineering|DevOps|SRE|on-?call|the (?:[\w-]+ )?(?:platform|infra\w*|ops|"
    r"backend|devops) team)\s+(?:deploys?|sets?|points?|blocks?|configures?|breaks?|stops?|takes? down|"
    r"reconfigures?|forces?)\b", re.I)
_TEAM_RE = re.compile(r"\b((?:the\s+)?[A-Za-z][\w-]*(?:\s+[A-Za-z][\w-]*)?\s+team|Engineering|DevOps|SRE|"
                      r"on-?call(?: engineer)?)\b", re.I)
_TEST_REF_RE = re.compile(r"›|\b[\w./-]+\.(?:test|spec)\.[jt]sx?\b|\b[\w./-]*test_[\w]+\.py\b|\b[\w./-]+_test\.go\b")
_ERROR_STATUS_RE = re.compile(r"(?<![\w.-])([45]\d\d)(?![\w.])")
_EXPECTED_SEG_RE = re.compile(r"(Expected(?:\s+result)?:\s*)(.*?)(?=\s+(?:Check|Verify(?:\s+(?:by|in|at))?|Confirm\s+in|"
                              r"Look\s+at|Where\s+to\s+look):|\Z)", re.I | re.S)
_STEPS_RE = re.compile(r"\bSteps?:", re.I)
_HEAD_RE = re.compile(r"^(\s*(?:🔴|🟡|🟢)\s*)")
_LINE_PREFIX_RE = re.compile(r"^(?:\s*(?:Precondition: Build check passed[^.]*\.|Unverified — [^:]{0,60}:|"
                             r"[^.:]{0,60}\(rule [A-Z]{1,2}\)[^—]*—))+\s*")


def _case_text(case: dict) -> str:
    req = case.get("request") if isinstance(case.get("request"), dict) else {}
    parts = [case.get("title"), case.get("preconditions"), case.get("credentials"), case.get("test_data"),
             case.get("blocked_by_setup")]
    # Rule AF's setup list names who has to act; AF copies it into preconditions only after this runs.
    for need in case.get("required_setup") or []:
        if isinstance(need, dict):
            parts += [need.get("need"), need.get("how_to_get")]
    parts += [s for s in case.get("steps") or [] if isinstance(s, str)]
    parts += [req.get("auth"), req.get("params")]
    return " ".join(_text(p) for p in parts)


def _case_calls(case: dict) -> list[tuple[str, str]]:
    out = []
    req = case.get("request") if isinstance(case.get("request"), dict) else None
    if req and _text(req.get("route")).startswith("/"):
        method = _text(req.get("method")).upper()
        if method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            out.append((method, _text(req.get("route")).split("?")[0]))
    for s in case.get("steps") or []:
        if isinstance(s, str):
            out += _calls(s)
    return list(dict.fromkeys(out))


def _explicitly_none(case: dict) -> bool:
    req = case.get("request") if isinstance(case.get("request"), dict) else {}
    for v in (_text(req.get("auth")), _text(case.get("credentials"))):
        if re.match(r"\s*(?:none|no\b|anonymous|unauthenticated|without)", v, re.I):
            return True
    return False


def _auth_stated(case: dict, text: str) -> bool:
    req = case.get("request") if isinstance(case.get("request"), dict) else {}
    if _text(req.get("auth")).strip():
        return True
    cred = _text(case.get("credentials")).strip()
    if cred and not re.fullmatch(r"(?:n/?a|unknown|tbd|-)", cred, re.I):
        return True
    return bool(_AUTH_STATED_RE.search(text))


# --- what the plan and the code say about a route ------------------------------

class RouteFacts:
    """Per-route evidence: authenticated, public or unknown, and from where."""

    def __init__(self, test_plan: Any, index=None):
        self.plan_auth: list[tuple[str, str, str, int]] = []    # (route, where, status, id of the case it came from)
        self.plan_public: list[str] = []
        self.creds: dict[str, Counter] = {}
        self.hooks = _auth_hooks(index)
        self.index = index
        for section, _, case in _all_cases(test_plan):
            calls = _case_calls(case)
            exp = _text(case.get("expected"))
            if _explicitly_none(case):
                status = re.search(r"\b(401|403)\b", exp)
                if status or re.search(r"\binvalid_token\b|\bunauthori[sz]ed\b|\btoken is required\b", exp, re.I):
                    for _, path in calls:
                        title = _text(case.get("title"))[:80]
                        code = status.group(1) if status else "401"
                        self.plan_auth.append((path, f"the plan's own no-credentials case “{title}” expects {code}",
                                               code, id(case)))
                elif re.search(r"\b(?:HTTP\s*)?200\b|\bis public\b", exp, re.I):
                    self.plan_public += [p for _, p in calls]
            elif not re.match(r"\s*$", _text(case.get("credentials"))):
                for _, path in calls:
                    self.creds.setdefault(path, Counter())[_text(case.get("credentials")).strip()] += 1
            for r in case.get("route_sources") or []:
                if not isinstance(r, dict):
                    continue
                for g in r.get("guards") or []:
                    name = _text(g.get("name") if isinstance(g, dict) else g)
                    if re.search(r"auth|token|jwt|session|login|bearer|signed|requireUser|protected", name, re.I):
                        src = _text(g.get("source")) if isinstance(g, dict) else ""
                        self.plan_auth.append((_text(r.get("path")), f"the {name} guard"
                                               + (f" at {src}" if src else ""), "401", 0))

    def authenticated(self, path: str, asking: dict | None = None) -> tuple[str, str] | None:
        """(where, status) when evidence shows ``path`` needs credentials. A case is never its own evidence."""
        if self.public(path):
            return None
        # Code first: it cites the file and line a tester or reviewer can open.
        for h in self.hooks:
            if h["registers"](path) and not any(route_matches(p, path) for p in h["public"]):
                return h["where"], h["status"]
        for route, where, status, source in self.plan_auth:
            if route and route_matches(route, path) and (asking is None or source != id(asking)):
                return where, status
        return None

    def public(self, path: str) -> bool:
        if any(route_matches(p, path) for p in self.plan_public):
            return True
        return any(h["registers"](path) and any(route_matches(p, path) for p in h["public"]) for h in self.hooks)

    def credentials_for(self, path: str) -> str | None:
        for route, c in self.creds.items():
            if route_matches(route, path) and c:
                return c.most_common(1)[0][0]
        return None


def _all_cases(test_plan: Any) -> Iterable[tuple[str, int, dict]]:
    for section in ALL_CASE_SECTIONS:
        for i, case in enumerate(getattr(test_plan, section, None) or []):
            if isinstance(case, dict):
                yield section, i, case


_HOOK_RE = re.compile(
    r"addHook\(\s*['\"](onRequest|preParsing|preValidation|preHandler)['\"]|"
    r"\b(?:app|server|router|api|fastify|instance)\.use\(|\bAPP_GUARD\b|@UseGuards\(|\bbefore_request\b|"
    r"\bMIDDLEWARE\b|\bdependencies=\[", re.I)
_AUTH_WORD_RE = re.compile(r"\b(?:auth\w*|bearer\w*|jwt\w*|verify\w*|passport|credential\w*|session\w*|"
                           r"token\w*)\b", re.I)


def _source_files(index) -> list[Any]:
    files = getattr(index, "files", None)
    if not isinstance(files, dict):
        return []
    return [f for f in files.values() if isinstance(getattr(f, "text", None), str)]


def _auth_hooks(index) -> list[dict]:
    """Global auth hooks/middleware in the code that was read, with the paths each exempts."""
    out = []
    for f in _source_files(index):
        if _is_test_path(f.path):
            continue
        lines = f.text.splitlines()
        for n, line in enumerate(lines):
            hm = _HOOK_RE.search(line)
            if not hm:
                continue
            # A hook is judged by its own body; middleware passed to use()/a
            # guard list by the name on its own line ("app.use(express.json())"
            # followed by unrelated token code is not an auth hook).
            window = _hook_body(lines, n) if hm.group(1) or "before_request" in line else line
            if not _AUTH_WORD_RE.search(window):
                continue
            public = _public_paths(f.text, window)
            m = re.search(r"\b(?:code|status)\(\s*(40[13])\s*\)", window)
            status = m.group(1) if m else "401"
            if not m and re.search(r"challenge|WWW-Authenticate|invalid_token", f.text):
                status = "401"
            repo, text = f.repo, f.text

            def registers(path: str, _repo=repo, _text_=text) -> bool:
                return _registers(index, _repo, _text_, path)

            out.append({"where": f"the auth hook at {f.path}:{n + 1} runs first", "status": status, "public": public,
                        "registers": registers})
            break
    return out


def _hook_body(lines: list[str], n: int, limit: int = 40) -> str:
    """The hook's lines, up to the line that closes it (indentation back to the hook's own)."""
    indent = len(lines[n]) - len(lines[n].lstrip())
    out = [lines[n]]
    for line in lines[n + 1:n + limit]:
        out.append(line)
        stripped = line.lstrip()
        if stripped and len(line) - len(stripped) <= indent and stripped[0] in "})]":
            break
    return "\n".join(out)


_EXEMPT_LINE_RE = re.compile(r"includes|startsWith|===|==|\bin\b|public|exempt|skip|allow|ignore|whitelist", re.I)


def _public_paths(file_text: str, window: str) -> list[str]:
    paths = [p for line in window.splitlines() if _EXEMPT_LINE_RE.search(line)
             for p in re.findall(r"['\"`](/[\w./{}:-]*)['\"`]", line)]
    exempt = "\n".join(line for line in window.splitlines() if _EXEMPT_LINE_RE.search(line))
    for const in re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", exempt):
        m = re.search(rf"\b{const}\s*=\s*[\[(\{{]([^\]\)\}}]*)", file_text)
        if m:
            paths += re.findall(r"['\"`](/[\w./{}:-]*)['\"`]", m.group(1))
    return list(dict.fromkeys(paths))


def _registers(index, repo: str, hook_file_text: str, path: str) -> bool:
    """A route literal for ``path`` is registered in the hook's file or in another read file of its repo."""
    rx = re.compile(r"['\"`]" + re.escape(path.rstrip("/") or "/") + r"/?['\"`]")
    if rx.search(hook_file_text):
        return True
    return any(f.repo == repo and not _is_test_path(f.path) and rx.search(f.text) for f in _source_files(index))


# --- unit tests -----------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"(?:^|/)(?:test|tests|__tests__|spec|specs)/|\.(?:test|spec)\.[cm]?[jt]sx?$|"
                           r"(?:^|/)test_[\w]+\.py$|_test\.(?:py|go)$|Tests?\.(?:kt|swift|java|cs)$")
_TEST_DECL_RE = re.compile(r"\b(?:it|test|specify)\s*\(\s*(['\"`])(.+?)\1|\bdef (test_\w+)\s*\(|"
                           r"\bfun\s+`([^`]+)`|\bfunc (Test\w+)\s*\(")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path or ""))


def _test_blocks(index, repo_evidence: list[dict] | None) -> list[tuple[str, str, str]]:
    """(file, test name, body) for every test in the test files read."""
    sources: dict[str, str] = {}
    for f in _source_files(index):
        if _is_test_path(f.path):
            sources[f.path] = f.text
    for e in repo_evidence or []:
        if isinstance(e, dict):
            for p, t in (e.get("test_sources") or {}).items():
                if isinstance(t, str) and _is_test_path(p):
                    sources.setdefault(p, t)
    out = []
    for path, text in sources.items():
        decls = list(_TEST_DECL_RE.finditer(text))
        for i, m in enumerate(decls):
            name = next(g for g in (m.group(2), m.group(3), m.group(4), m.group(5)) if g)
            end = decls[i + 1].start() if i + 1 < len(decls) else len(text)
            out.append((path, name, text[m.start():end]))
    return out


def _tokens(expected: str) -> tuple[set[str], set[str]]:
    """(statuses, other distinctive tokens) a test must assert to prove ``expected``."""
    statuses = set(_ERROR_STATUS_RE.findall(expected))
    toks = set(re.findall(r"-32\d{3}\b", expected))
    toks |= {t.lower() for t in re.findall(r"\b[a-z]+_[a-z_]+\b", expected)}
    toks |= {t.lower() for t in re.findall(r"\b[A-Z][a-z]+(?:-[A-Z][a-z]+)+\b", expected)}  # Retry-After
    return statuses, toks


def find_asserting_test(expected: str, blocks: list[tuple[str, str, str]], title: str = "") -> str | None:
    """``<file> › <test name>`` of the test that asserts ``expected``'s status and markers, or None."""
    statuses, toks = _tokens(expected)
    if not statuses and not toks:
        return None
    title_words = {w for w in re.findall(r"[a-z]{4,}", title.lower())}
    best, best_score = None, 0
    for path, name, body in blocks:
        low = body.lower()
        s_hits = sum(1 for s in statuses if re.search(rf"(?<!\d){s}(?!\d)", body))
        t_hits = sum(1 for t in toks if t in low)
        if statuses and not s_hits:
            continue
        name_hits = sum(1 for w in title_words if w in name.lower())
        score = s_hits * 3 + t_hits * 2 + name_hits
        if score > best_score and (s_hits + t_hits) >= min(2, len(statuses) + len(toks)):
            best, best_score = f"{path} › {name}", score
    return best


# --- topology ---------------------------------------------------------------------

def edge_in_front(test_plan: Any) -> tuple[str, str, str]:
    """("shown"|"unknown"|"none", the edge's name, what sits behind it)."""
    topo = [t for t in getattr(test_plan, "environment_topology", None) or [] if isinstance(t, dict)]
    text = " ".join(_text(t.get("in_front")) + " " + _text(t.get("in_front_source")) for t in topo)
    proxy = _PROXY_RE.search(text)
    behind = f"{proxy.group(1)} and the app" if proxy else "the app"
    # "no CDN" names the term too: strip what is ruled out before looking for what is there.
    shown = _EDGE_TERM_RE.search(_NO_EDGE_RE.sub(" ", text))
    if shown:
        name = shown.group(0)
        name = "Cloudflare" if re.match(r"cloudflare|server: ?cloudflare|cf-ray", name, re.I) else name
        return "shown", name, behind
    if topo and _NO_EDGE_RE.search(text):
        return "none", "", behind
    return "unknown", "a CDN or WAF in front of the public hostname (the topology does not rule one out)", behind


# --- edits --------------------------------------------------------------------------

def _line_expected(line: str) -> str:
    m = _EXPECTED_SEG_RE.search(line)
    return m.group(2).strip() if m else ""


def _replace_line_expected(line: str, new: str) -> str:
    m = _EXPECTED_SEG_RE.search(line)
    if not m:
        return f"{line} Expected: {new}"
    return line[:m.start(2)] + new + line[m.end(2):]


def _insert_before_steps(line: str, note: str) -> str:
    m = _STEPS_RE.search(line)
    if m:
        return f"{line[:m.start()]}{note} {line[m.start():]}"
    h = _HEAD_RE.match(line)
    head = h.group(1) if h else ""
    return f"{head}{note} {line[len(head):]}"


def _principal_and_auth(facts: RouteFacts, path: str, test_plan: Any) -> tuple[str, str]:
    cred = facts.credentials_for(path)
    if not cred:
        any_cred = Counter(_text(c.get("credentials")).strip() for _, _, c in _all_cases(test_plan)
                           if _text(c.get("credentials")).strip() and not _explicitly_none(c))
        cred = any_cred.most_common(1)[0][0] if any_cred else None
    if cred:
        return f"the same principal as the plan's other {path} cases", cred.rstrip(".")
    return ("a test user who is allowed to call it (the plan names none)",
            "a valid token for that user, with the scopes the route needs")


def _strip_rule_marks(text: str) -> str:
    return re.sub(r"\s*\([^()]*\(rule [A-Z]{1,2}\)[^()]*\)|\b[^.—]*\(rule [A-Z]{1,2}\)[^—]*—\s*", " ", text).strip()


# --- the check ------------------------------------------------------------------------

def check_request_layers(test_plan: Any, report: dict, index=None,
                         repo_evidence: list[dict] | None = None) -> int:
    """Rule AM over every section. Marks and fixes in place; returns the number of cases/lines changed."""
    facts = RouteFacts(test_plan, None if isinstance(index, str) else index)
    blocks = _test_blocks(None if isinstance(index, str) else index, repo_evidence)
    edge = edge_in_front(test_plan)
    changed = 0
    for section, _, case in _all_cases(test_plan):
        changed += _check_case(section, case, facts, blocks, edge, test_plan, report)
    lines = getattr(test_plan, "regression_checklist", None)
    if isinstance(lines, list):
        for i, line in enumerate(lines):
            if isinstance(line, str):
                new = _check_line(line, facts, blocks, edge, test_plan, report)
                if new != line:
                    lines[i] = new
                    changed += 1
    return changed


def _edge_rewrite(expected: str, edge: tuple[str, str, str], blocks, title: str) -> tuple[str, str | None]:
    _, name, behind = edge
    test = find_asserting_test(expected, blocks, title)
    original = _strip_rule_marks(expected).rstrip(".") or "its rejection"
    proof = (f"asserted by {test}" if test else
             "not externally verifiable: it is only reachable at the origin, and no test asserting it was found")
    return EDGE_EXPECTED.format(edge=name, behind=behind, original=original, proof=proof), test


def _check_case(section: str, case: dict, facts: RouteFacts, blocks, edge, test_plan, report) -> int:
    calls = _case_calls(case)
    text = _case_text(case)
    exp = _text(case.get("expected"))
    changed = False
    title = _text(case.get("title"))
    # 4. infrastructure-only trigger
    if _infra_only(text) and not _TEST_REF_RE.search(text + " " + exp + " " + _text(case.get("unit_test_ref"))) \
            and "(rule AM)" not in exp:
        test = find_asserting_test(exp, blocks, title)
        note = _infra_note(test, text)
        case["expected"] = f"{exp} {note}".strip()
        if test and not case.get("unit_test_ref"):
            case["unit_test_ref"] = test
        _record(report, case, section, RULE, "marked",
                f"its only trigger is an action a tester cannot take; {'covered by ' + test if test else 'manual, ' + _owner(text)}",
                "infrastructure-only trigger")
        changed = True
    path = calls[0][1] if calls else ""
    auth = facts.authenticated(path, case) if calls else None
    subject_auth = bool(_AUTH_SUBJECT_RE.search(title + " " + text))
    # 2. deliberately unauthenticated, but expects a later check's answer
    if not calls:
        pass
    elif auth and _explicitly_none(case) and "(rule AM)" not in case.get("expected", ""):
        # Route-not-found, method-not-allowed and rate limits can answer before an auth hook does.
        statuses = set(_ERROR_STATUS_RE.findall(_text(case.get("expected")))) - {"404", "405", "429"}
        if statuses and not statuses & {"401", "403"}:
            case["expected"] = f"{AUTH_FIRST_MARKER.format(where=auth[0], status=auth[1])} {case['expected']}"
            case["expected_verified"] = False
            _record(report, case, section, RULE, "marked",
                    f"no credentials, so {auth[0]} answers {auth[1]} first", "auth layer answers first")
            changed = True
    # 1. auth stated
    elif auth and not subject_auth and not _auth_stated(case, text):
        principal, how = _principal_and_auth(facts, path, test_plan)
        note = AUTH_NOTE.format(principal=principal, auth=how, route=path, where=auth[0], status=auth[1])
        case["preconditions"] = f"{note} {_text(case.get('preconditions'))}".strip()
        if not _text(case.get("credentials")).strip():
            case["credentials"] = how
        _record(report, case, section, RULE, "marked",
                f"{path} is authenticated ({auth[0]}) and the case named no principal; added one", "auth not stated")
        changed = True
    # 5. order unknown
    elif not auth and not facts.public(path) and not _auth_stated(case, text) and not subject_auth \
            and "(rule AM)" not in case.get("expected", ""):
        statuses = set(_ERROR_STATUS_RE.findall(_text(case.get("expected"))))
        if statuses:
            case["expected"] = f"{ORDER_MARKER.format(route=path)} {case['expected']}"
            case["expected_verified"] = False
            _record(report, case, section, RULE, "marked", f"no principal and the order of checks on {path} is unknown",
                    "order of checks unverified")
            changed = True
    # 3. edge answers first
    exp = _text(case.get("expected"))
    if _EDGE_INPUT_RE.search(text) and edge[0] != "none" and not _EDGE_TERM_RE.search(text + " " + exp) \
            and not ORIGIN_DIRECT_RE.search(text) and "(rule AC)" not in exp:
        new, test = _edge_rewrite(exp, edge, blocks, title)
        case["expected"] = new
        case["expected_verified"] = False
        if test and not case.get("unit_test_ref"):
            case["unit_test_ref"] = test
        _record(report, case, section, RULE, "marked",
                f"{edge[1]} may answer a header-level input before the app; app status "
                + (f"asserted by {test}" if test else "not externally verifiable"), "edge answers first")
        changed = True
    return int(changed)


def _infra_only(text: str) -> bool:
    """Its trigger is an infrastructure action no tester can take. "Take the browser offline (or have
    Engineering force the call to fail)" offers the tester a way, so it does not count."""
    return any(not re.search(r"\bor\s+(?:have\s+|ask\s+)?$", text[max(0, m.start() - 14):m.start()], re.I)
               for m in _INFRA_RE.finditer(text))


def _infra_note(test: str | None, text: str) -> str:
    owner = _owner(text)
    named = not owner.endswith("(not named in the plan)")
    if test and named and not re.search(r"\bonly if\b", text, re.I):
        return INFRA_TEST_AND_OWNER_NOTE.format(test=test, owner=owner)
    return INFRA_TEST_NOTE.format(test=test) if test else INFRA_MANUAL_NOTE.format(owner=owner)


def _owner(text: str) -> str:
    m = _TEAM_RE.search(text)
    if m and not re.match(r"(?:the\s+)?(?:same|test|this|that|qa)\s+team", m.group(1), re.I):
        t = m.group(1)
        if not re.search(r"team$", t, re.I):
            return {"engineering": "Engineering", "devops": "DevOps", "sre": "SRE"}.get(t.lower(), t)
        return t if t.lower().startswith("the ") else f"the {t}"
    return "the team that runs the dependency (not named in the plan)"


_MENTION_RE = re.compile(r"\b(?:referenc\w*|mention\w*|grep\w*|search(?:es|ing)? (?:for|the)|spec(?:s)? (?:still )?"
                         r"(?:call|use)s?|calls? to|occurrences? of)\W*(?:\w+\W+){0,3}$", re.I)


def _sent_calls(text: str) -> list[tuple[str, str]]:
    """Requests the line sends, not ones it only mentions ("confirm no spec still references PUT /x")."""
    out = []
    for method, path in _calls(text):
        at = text.find(f"{method} {path}")
        if at >= 0 and _MENTION_RE.search(text[max(0, at - 60):at]):
            continue
        out.append((method, path))
    return out


def _check_line(line: str, facts: RouteFacts, blocks, edge, test_plan, report) -> str:
    orig = line
    exp = _line_expected(line)
    title = re.split(r"\s+—\s+|\s+-\s+at risk", _LINE_PREFIX_RE.sub("", _HEAD_RE.sub("", line)), 1)[0][:140]
    # Before Expected: the area, the at-risk citation, a "Runs as:" note and the steps.
    pre = line[:line.find("Expected:")] if "Expected:" in line else line
    # What the tester does is in the steps; "at risk via … the cron GET /" is prose, not a request.
    st = _STEPS_RE.search(pre)
    steps_part = pre[st.end():] if st else pre
    # 4. infrastructure-only trigger
    if _infra_only(steps_part) and not _TEST_REF_RE.search(line) and "(rule AM)" not in line:
        test = find_asserting_test(exp, blocks, title)
        note = _infra_note(test, line)
        line = _replace_line_expected(line, f"{exp.rstrip()} {note}".strip())
        _record(report, None, "regression_checklist", RULE, "marked",
                f"its only trigger is an action a tester cannot take; "
                f"{'covered by ' + test if test else 'manual, ' + _owner(orig)}", title)
    calls = _sent_calls(steps_part)
    if not calls:
        return line
    path = calls[0][1]
    auth = facts.authenticated(path)
    subject_auth = bool(_AUTH_SUBJECT_RE.search(pre))
    if auth and not subject_auth and not _AUTH_STATED_RE.search(pre):
        principal, how = _principal_and_auth(facts, path, test_plan)
        line = _insert_before_steps(line, AUTH_NOTE.format(principal=principal, auth=how, route=path,
                                                           where=auth[0], status=auth[1]))
        _record(report, None, "regression_checklist", RULE, "marked",
                f"{path} is authenticated ({auth[0]}) and the line named no principal; added one", title)
    elif not auth and not facts.public(path) and not subject_auth and not _AUTH_STATED_RE.search(pre) \
            and _ERROR_STATUS_RE.search(exp) and "(rule AM)" not in exp:
        line = _replace_line_expected(line, f"{ORDER_MARKER.format(route=path)} {exp}")
        _record(report, None, "regression_checklist", RULE, "marked",
                f"no principal and the order of checks on {path} is unknown", title)
    exp = _line_expected(line)
    if _EDGE_INPUT_RE.search(steps_part) and edge[0] != "none" and not _EDGE_TERM_RE.search(orig) \
            and not ORIGIN_DIRECT_RE.search(pre) and "(rule AC)" not in line:
        new, test = _edge_rewrite(exp, edge, blocks, title)
        line = _replace_line_expected(line, new)
        _record(report, None, "regression_checklist", RULE, "marked",
                f"{edge[1]} may answer a header-level input before the app; app status "
                + (f"asserted by {test}" if test else "not externally verifiable"), title)
    return line
