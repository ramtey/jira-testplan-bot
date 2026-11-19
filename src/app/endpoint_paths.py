"""Rule AI: every endpoint a case names is its full mounted path, or is marked unverified.

A plan's security cases named a tRPC procedure ``reports.exportBundle`` — the
key it has in the router file that defines it. The callable path was
``/trpc/admin.reports.exportBundle``: the router is mounted as ``reports`` in
an admin router, that one as ``admin`` in the root router, and the server
serves the root under ``/trpc``. Every call the tester made got "No procedure
found on path", so the no-token case "passed" because the route was missing,
not because auth rejected it, and the QA runner fixed the paths by hand.

The prompt has rule AI for this. This module does not trust the generator to
have followed it. For each endpoint a case names (an HTTP call in a step, a
tRPC procedure, a security case's ``request.route``):

1. the path is traced from its definition to the server entry by reading the
   code (``mount_tracer``), and compared with the chain the plan cites in
   ``route_sources[].mount_chain``;
2. a real caller the plan cites (``caller_source``) is read and must call the
   same path;
3. when the traced path is known and nothing disputes it, a partial name in
   the steps, expected result and ``request.route`` is rewritten to the full
   path, and the chain is written into "How to verify";
4. when it is not, or the code and the plan (or the caller) disagree, the case
   is marked "Endpoint path unverified (rule AI)" with what is missing, and
   ``expected_verified`` is cleared — no path is picked silently;
5. guards on the chain (an admin-only procedure builder, middleware on a
   prefix) are carried into the case's preconditions;
6. a negative case (no token, a bad token, the wrong role) says how to tell
   "rejected by the check" from "route not found", with the status and error
   shape of each; one that cannot is downgraded.

Every rewrite and downgrade is recorded in ``source_provenance["plan_recheck"]``
under rule AI, which the plan's "Grounding re-read" line lists. Code it cannot
read never flags on its own: the plan's cited chain stands when each link's
key is in its cited file, and the trace that could not finish is noted.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import mount_tracer
from .plan_recheck import _cases, _record

logger = logging.getLogger(__name__)

UNVERIFIED_MARKER = ("Endpoint path unverified (rule AI): {why} — confirm the path from a real client call (the "
                     "app's network tab or a client hook) before grading this case —")
DISPUTED_MARKER = ("Endpoint path disputed (rule AI): {why} — confirm which one a real client calls before grading "
                   "this case —")
TRACED_NOTE = "Endpoint path traced (rule AI): {path} — {chain}."
CALLER_NOTE = "Same path the client calls: {caller}."
GUARD_NOTE = "Guard on this path (rule AI): {guards}."
NOT_FOUND_NOTE = ("If the response is {status} with {shape}, the route was not found: the path is wrong, not the "
                  "check passing — fix the path before grading.")
REJECT_NOTE = "Rejected by the check under test looks like: {status}, {shape}."
NO_SIGNATURE_WHY = ("a negative case with no expected status or error for the rejection cannot be told apart from "
                    "the route not being found")

MAX_TRACES = 16

_NEGATIVE_RE = re.compile(
    r"\b(?:no|without\s+(?:a|an|the)?|missing|invalid|garbage|malformed|expired|forged|bad|wrong|revoked|tampered)\s+"
    r"(?:auth(?:orization)?\s+)?(?:header|token|jwt|bearer|session|cookie|credentials?|signature|role|scope)\b"
    r"|\bnon[- ]?admin\b|\bunauthori[sz]ed\b|\bunauthenticated\b|\bforbidden\b|\b40[134]\b|\bnot\s+allowed\b"
    r"|\b(?:another|second|other|different)\s+(?:user|tenant|account|org(?:ani[sz]ation)?|agent|brokerage)\b"
    r"|\bnot\s+the\s+owner\b"
    r"|\b(?:is|are|gets?|be)\s+(?:rejected|denied|refused)\b|\baccess\s+denied\b", re.I)
_PROC_RE = re.compile(r"(?<![\w$./-])((?:/[\w-]+)+/)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){1,7})(?![\w$(])")
_CLIENT_HEAD = {"api", "trpc", "client", "utils", "ctx", "caller", "trpcclient", "apiclient", "rpc"}
_CLIENT_TAIL = {"usequery", "usemutation", "usesuspensequery", "useinfinitequery", "query", "mutate", "mutateasync",
                "fetch", "prefetch", "invalidate", "getdata", "setdata", "subscribe", "usesubscription"}
_NOT_ENDPOINT_HEAD = {"res", "req", "request", "response", "body", "data", "process", "window", "console", "this",
                      "json", "error", "input", "ctx", "env", "config", "user", "session", "result", "e", "err",
                      "document", "location", "headers", "params", "query", "payload", "z"}
_FILE_TAIL = re.compile(r"^(?:[cm]?[jt]sx?|json|ya?ml|md|py|kt|swift|java|go|rb|cs|html|css|png|pdf|zip|com|io|net|"
                        r"org|dev|test|app|xml|txt|csv|env|lock|sh)$", re.I)
_STATUS_RE = re.compile(r"\b([245]\d\d)\b(?!\s*(?:ms|milliseconds?|s|seconds?|mi?b|kb|bytes|requests?)\b|\s*%)", re.I)
_TRPC_ERR_RE = re.compile(r"\b(UNAUTHORIZED|FORBIDDEN|BAD_REQUEST|NOT_FOUND|TOO_MANY_REQUESTS|PRECONDITION_FAILED|"
                          r"CONFLICT|UNPROCESSABLE_CONTENT|METHOD_NOT_SUPPORTED)\b")
_TRPC_STATUS = {"UNAUTHORIZED": "401", "FORBIDDEN": "403", "BAD_REQUEST": "400", "NOT_FOUND": "404",
                "TOO_MANY_REQUESTS": "429", "PRECONDITION_FAILED": "412", "CONFLICT": "409",
                "UNPROCESSABLE_CONTENT": "422", "METHOD_NOT_SUPPORTED": "405"}


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _file_text(index, locator: str) -> str | None:
    from .deployed_env_checks import _file_text as ft

    return ft(index, locator)


# ---------------------------------------------------------------------------
# What a case names
# ---------------------------------------------------------------------------

def _dotted(path: str) -> str:
    """The procedure part of a tRPC path: ``/trpc/admin.x.y`` -> ``admin.x.y``."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def _is_procedure_path(path: str) -> bool:
    last = _dotted(path)
    return "." in last and not _FILE_TAIL.match(last.rsplit(".", 1)[-1])


def _client_form(head: str, dotted: str) -> str | None:
    """``api.admin.x.y.useMutation`` -> ``admin.x.y``; None when it is not a client call."""
    parts = dotted.split(".")
    if head or parts[0].lower() not in _CLIENT_HEAD:
        return None
    parts = parts[1:]
    while parts and parts[-1].lower() in _CLIENT_TAIL:
        parts = parts[:-1]
    return ".".join(parts) if len(parts) >= 1 else None


def _procedure_mentions(text: str, leaves: set[str]) -> list[tuple[str, str, bool]]:
    """``(mention as written, procedure path, is_client_form)`` for dotted names whose last part is a known leaf."""
    out = []
    for m in _PROC_RE.finditer(text):
        head, dotted = m.group(1) or "", m.group(2)
        client = _client_form(head, dotted)
        proc = client if client is not None else dotted
        parts = proc.split(".")
        if not parts or parts[-1] not in leaves or _FILE_TAIL.match(parts[-1]):
            continue
        if client is None and parts[0].lower() in _NOT_ENDPOINT_HEAD and not head:
            continue
        out.append((m.group(0), (head + proc) if head else proc, client is not None))
    return out


def _route_entries(case: dict) -> list[dict]:
    return [r for r in case.get("route_sources") or [] if isinstance(r, dict) and _text(r.get("path")).strip()]


def _texts(case: dict) -> list[str]:
    req = case.get("request") if isinstance(case.get("request"), dict) else {}
    return [s for s in (case.get("steps") or []) if isinstance(s, str)] + [_text(case.get("expected")),
                                                                           _text(req.get("route"))]


def _endpoints(case: dict) -> list[dict]:
    """Each endpoint the case names, grouped by procedure leaf (tRPC) or by HTTP path."""
    from .deployed_env_checks import _calls

    entries = _route_entries(case)
    req = case.get("request") if isinstance(case.get("request"), dict) else {}
    req_route = _text(req.get("route")).strip()
    trpc_hint = (_text(req.get("protocol")).lower() == "trpc" or _text(req.get("method")).lower() in ("query", "mutation")
                 or any(_is_procedure_path(_text(r.get("path"))) for r in entries))
    leaves = {_dotted(_text(r.get("path"))).rsplit(".", 1)[-1] for r in entries if _is_procedure_path(_text(r.get("path")))}
    if req_route and (_is_procedure_path(req_route) or trpc_hint) and "." in _dotted(req_route):
        leaves.add(_dotted(req_route).rsplit(".", 1)[-1])
    elif req_route and trpc_hint and "/" not in req_route:
        leaves.add(req_route)
    eps: dict[str, dict] = {}
    for text in _texts(case):
        for written, proc, client in _procedure_mentions(text, leaves):
            leaf = proc.rsplit(".", 1)[-1]
            ep = eps.setdefault("trpc:" + leaf, {"kind": "trpc", "leaf": leaf, "mentions": [], "method": None})
            ep["mentions"].append((written, proc, client))
    for text in [s for s in (case.get("steps") or []) if isinstance(s, str)]:
        for method, path in _calls(text):
            if _is_procedure_path(path):
                leaf = _dotted(path).rsplit(".", 1)[-1]
                ep = eps.setdefault("trpc:" + leaf, {"kind": "trpc", "leaf": leaf, "mentions": [], "method": method})
                ep["mentions"].append((path, path, False))
                continue
            ep = eps.setdefault("http:" + method + " " + path,
                                {"kind": "http", "leaf": path, "mentions": [], "method": method})
            ep["mentions"].append((path, path, False))
    if req_route and not any(req_route in [w for w, _, _ in e["mentions"]] for e in eps.values()):
        if "/" in req_route and not _is_procedure_path(req_route):
            key = "http:" + _text(req.get("method")).upper() + " " + req_route
            eps.setdefault(key, {"kind": "http", "leaf": req_route, "mentions": [],
                                 "method": _text(req.get("method")).upper() or None})["mentions"].append(
                (req_route, req_route, False))
        elif trpc_hint or _is_procedure_path(req_route):
            leaf = _dotted(req_route).rsplit(".", 1)[-1]
            eps.setdefault("trpc:" + leaf, {"kind": "trpc", "leaf": leaf, "mentions": [], "method": None})[
                "mentions"].append((req_route, req_route, False))
    return list(eps.values())


def _entry_for(ep: dict, entries: list[dict]) -> dict | None:
    from .deployed_env_checks import route_matches

    if ep["kind"] == "trpc":
        hits = [r for r in entries if _is_procedure_path(_text(r.get("path")))
                and _dotted(_text(r.get("path"))).rsplit(".", 1)[-1] == ep["leaf"]]
        hits = hits or [r for r in entries if _dotted(_text(r.get("path"))) == ep["leaf"]]
    else:
        path = ep["leaf"]
        hits = [r for r in entries if route_matches(_text(r.get("path")), path)]
        hits = hits or [r for r in entries if _suffix_match(_text(r.get("path")), path)]
        if ep.get("method"):
            hits = [r for r in hits if _text(r.get("method")).upper() in (ep["method"], "ALL", "ANY", "")] or hits
    return hits[0] if hits else None


def _segs(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


_VALUE_SEG = re.compile(r"^(?::\w+\??|\{[^}]*\}|<[^>]*>|\[[^\]]*\]|\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{24,})$", re.I)


def _suffix_match(full: str, part: str) -> bool:
    """Is ``part`` the tail of ``full`` (by path segment)? A parameter in ``full``
    matches only a value-like segment (an id, a uuid, a placeholder), never a word."""
    from .deployed_env_checks import _PARAM_SEG_RE

    a, b = _segs(full.lower()), _segs(part.lower())
    if not 0 < len(b) <= len(a):
        return False
    for x, y in zip(a[-len(b):], b):
        if x == y or (_PARAM_SEG_RE.match(x) and _VALUE_SEG.match(y)) or (_PARAM_SEG_RE.match(y) and _PARAM_SEG_RE.match(x)):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# The plan's own chain
# ---------------------------------------------------------------------------

# Routing config that is the definition and the mount at once: the path it maps is the full path.
_SELF_MOUNTED = re.compile(r"(?:^|/)(?:firebase\.json|serverless\.ya?ml|template\.ya?ml|vercel\.json|netlify\.toml)"
                           r"(?::|\s|$)")


def _model_chain(entry: dict) -> list[dict]:
    chain = [c for c in entry.get("mount_chain") or [] if isinstance(c, dict) and _text(c.get("source"))]
    if not chain and _text(entry.get("mount_source")):
        chain = [{"key": "", "source": _text(entry.get("mount_source"))}]
    if not chain and _SELF_MOUNTED.search(_text(entry.get("source"))):
        chain = [{"key": "", "source": _text(entry.get("source"))}]
    return chain


def _defines(text: str, leaf: str) -> int | None:
    """Line where ``text`` defines the tRPC procedure ``leaf`` inside a router(...) call."""
    for m in re.finditer(rf"(?<![\w$.]){re.escape(leaf)}\s*:\s*[A-Za-z_$][\w$]*\s*(?:\.|\n|,)", text):
        if mount_tracer.enclosing_router(text, m.start()):
            return text.count("\n", 0, m.start()) + 1
    return None


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]", "", name).lower()


async def _find_procedure(index, github, mentions: list[str], leaf: str) -> str | None:
    """``<file>:<line>`` of the one file that defines the tRPC procedure ``leaf``:
    among the files read, else among the repo's files named after a router key
    in the mention (``gfeDebug`` -> ``gfe-debug.ts``)."""
    if index is None or isinstance(index, str):
        return None
    hits = []
    for (_, path), f in list(getattr(index, "files", {}).items()):
        line = _defines(getattr(f, "text", "") or "", leaf)
        if line and not re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", path):
            hits.append(f"{path}:{line}")
    if hits:
        return hits[0] if len(hits) == 1 else None
    keys = {_norm_name(k) for m in mentions for k in _dotted(m).split(".")[:-1] if k}
    if not keys:
        return None
    for repo, tree in (getattr(index, "trees", {}) or {}).items():
        cands = [p for p in tree or [] if p.endswith(mount_tracer.CODE_EXT)
                 and not re.search(r"\.(?:test|spec|d)\.[cm]?[jt]sx?$", p)
                 and (_norm_name(mount_tracer._stem(p)) in keys or (mount_tracer._stem(p) == "index" and
                      _norm_name(p.rsplit("/", 2)[-2] if p.count("/") else "") in keys))]
        if not cands or len(cands) > 6:
            continue
        files = mount_tracer._Files(index, github, repo)
        dirs = {p.rsplit("/", 1)[0] if "/" in p else "" for p in cands}
        for d in dirs:
            await files.dir_files(d, set())
        for p in cands:
            text = files.text(p)
            line = _defines(text, leaf) if text else None
            if line:
                hits.append(f"{p}:{line}")
    return hits[0] if len(hits) == 1 else None


def _chain_problem(chain: list[dict], index) -> str | None:
    """Why the plan's cited chain does not hold, or None. Unread links are not problems."""
    for c in chain:
        key = _text(c.get("key")).strip().strip("/")
        if not key:
            continue
        text = _file_text(index, _text(c.get("source")))
        if text is not None and key.split("/")[0].lower() not in text.lower():
            return f"'{key}' is not in {_text(c.get('source'))}, where the plan says it is mounted"
    return None


def _norm(path: str) -> str:
    p = path.strip().lower()
    return "/" + "/".join(_segs(p)) if p.startswith("/") or "/" in p else p


def _same_path(a: str, b: str) -> bool:
    from .deployed_env_checks import route_matches

    if _is_procedure_path(a) or _is_procedure_path(b):
        return _norm(a) == _norm(b)
    return route_matches(a, b) and route_matches(b, a)


def _is_full(path: str, kind: str) -> bool:
    """A tRPC path is full only with its URL prefix (or as a bare root-served name the trace confirmed)."""
    return path.startswith("/") if kind == "trpc" else True


# ---------------------------------------------------------------------------
# Rewrites and marks
# ---------------------------------------------------------------------------

def _replace_mention(text: str, written: str, full: str) -> str:
    if not text or written == full:
        return text
    if written.startswith("/"):
        # An HTTP path: keep its concrete values, add the missing leading segments
        # (also inside a URL, after the host).
        new = "/" + "/".join(_segs(full)[: len(_segs(full)) - len(_segs(written))] + _segs(written))
        return re.sub(rf"(^|[\s`\"'(]|https?://[^\s/`\"']+){re.escape(written)}(?![\w$/-])",
                      lambda m: m.group(1) + new, text)
    # The look-behind keeps a name that is already part of a longer path (or a client call) as it is.
    return re.sub(rf"(?<![\w$./-]){re.escape(written)}(?![\w$])", full, text)


def _relation(written: str, full: str, kind: str, prefixes: set[str]) -> str:
    """``same``, ``partial`` (a tail of the full path) or ``other``."""
    if _same_path(written, full):
        return "same"
    if kind == "trpc":
        w, f = _dotted(written).lower().split("."), _dotted(full).lower().split(".")
        if written.startswith("/") and _norm(written) != _norm(full):
            return "other"
        # Without the URL prefix, the same procedure (or a tail of it) is a partial name.
        return "partial" if len(w) <= len(f) and f[-len(w):] == w else "other"
    segs = _segs(written)
    if segs and segs[0].lower() in prefixes and _same_path("/" + "/".join(segs[1:]), full):
        return "same"
    return "partial" if _suffix_match(full, written) else "other"


def _rewrite(case: dict, written: str, full: str) -> bool:
    changed = False
    steps = case.get("steps") or []
    for i, s in enumerate(steps):
        if isinstance(s, str):
            new = _replace_mention(s, written, full)
            changed |= new != s
            steps[i] = new
    exp = _text(case.get("expected"))
    new = _replace_mention(exp, written, full)
    if new != exp:
        case["expected"], changed = new, True
    req = case.get("request")
    if isinstance(req, dict) and isinstance(req.get("route"), str):
        new = _replace_mention(req["route"], written, full)
        if new != req["route"]:
            req["route"], changed = new, True
    return changed


def _mark(case: dict, marker: str, needle: str) -> None:
    steps = case.get("steps") or []
    for i, s in enumerate(steps):
        if isinstance(s, str) and needle and needle in s:
            steps[i] = f"{marker} {s}"
            break
    else:
        case["expected"] = f"{marker} {_text(case.get('expected'))}".strip()
    case["expected_verified"] = False


def _append(case: dict, field_name: str, sentence: str) -> bool:
    cur = _text(case.get(field_name)).strip()
    if sentence in cur:
        return False
    case[field_name] = f"{cur} {sentence}".strip() if cur else sentence
    return True


def _has_marker(case: dict) -> bool:
    return any("(rule AI)" in t and ("unverified" in t or "disputed" in t) for t in _texts(case))


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

async def _trace(ep: dict, entry: dict | None, index, github, cache: dict) -> mount_tracer.Trace | None:
    if entry is None or index is None or isinstance(index, str):
        return None
    src = _text(entry.get("source"))
    if not src:
        return None
    if ep["kind"] == "trpc":
        args = {"procedure": ep["leaf"]}
    else:
        args = {"sub_path": None, "method": ep.get("method") or _text(entry.get("method")).upper() or None}
    key = (src, ep["leaf"], ep["kind"])
    if key in cache:
        return cache[key]
    if len(cache) >= MAX_TRACES:
        return None
    t = None
    try:
        if ep["kind"] == "trpc":
            t = await mount_tracer.trace(index, github, src, **args)
        else:
            # The definition registers a tail of the path: try the longest tail it has.
            segs = _segs(ep["leaf"])
            for k in range(max(len(segs), 1)):  # never the bare "/": that matches any catch-all
                sub = "/" + "/".join(segs[k:])
                t = await mount_tracer.trace(index, github, src, sub_path=sub, method=args["method"])
                if t is None or t.full_path or not (t.missing or "").startswith("no single route registration"):
                    break
    except Exception as e:  # noqa: BLE001 — a failed trace is "not traced", never "absent"
        logger.warning("mount trace failed for %s: %s", src, e)
        t = mount_tracer.Trace("unknown", missing=f"the mount trace failed ({type(e).__name__})")
    cache[key] = t
    return t


def _caller_problem(entry: dict, full: str, kind: str, index) -> tuple[str | None, str | None]:
    """``(disagreement, agreeing caller note)`` from the plan's cited caller."""
    src = _text(entry.get("caller_source"))
    said = _text(entry.get("caller_path")).strip()
    if not src and not said:
        return None, None
    if kind == "trpc":
        want = _dotted(full)
        if said:
            said_proc = _client_form("", said) or _dotted(said)
            if said_proc.lower() != want.lower():
                return f"the client at {src or 'the cited caller'} calls '{said}', the traced path is {full}", None
        text = _file_text(index, src) if src else None
        if text is not None and want not in text and want.replace(".", "?.") not in text:
            got = re.search(rf"\b(?:api|trpc|client)\.([\w.]*\b{re.escape(want.rsplit('.', 1)[-1])})\b", text)
            seen = f"calls '{got.group(1)}'" if got else f"has no call to '{want}'"
            return f"the client at {src} {seen}, the traced path is {full}", None
        return None, f"{src}" + (f" ({said})" if said else "")
    text = _file_text(index, src) if src else None
    statics = [s for s in _segs(full) if not s.startswith(":") and not s.startswith("{")]
    if said and not _same_path(said.split()[-1], full):
        return f"the client at {src or 'the cited caller'} calls '{said}', the traced path is {full}", None
    if text is not None and statics and not all(s.lower() in text.lower() for s in statics):
        miss = next(s for s in statics if s.lower() not in text.lower())
        return f"the client at {src} does not build '{miss}' of {full}", None
    return None, f"{src}" + (f" ({said})" if said else "")


def _guards(entry: dict | None, trace: mount_tracer.Trace | None) -> list[str]:
    out = []
    for g in (entry or {}).get("guards") or []:
        if isinstance(g, dict) and _text(g.get("name")):
            out.append(f"{g['name']} ({_text(g.get('source')) or 'source not cited'})")
        elif isinstance(g, str) and g.strip():
            out.append(g.strip())
    if trace is not None:
        for name, src in trace.guards:
            if not any(o.startswith(name + " ") or o == name for o in out):
                out.append(f"{name} ({src})")
    return out


def _framework(ep: dict, trace: mount_tracer.Trace | None, full: str | None) -> str:
    if trace is not None and trace.framework in mount_tracer.NOT_FOUND:
        return trace.framework
    if ep["kind"] == "trpc" or (full and "/trpc" in full):
        return "trpc"
    return ""


def _rejection(case: dict, ep: dict) -> tuple[str | None, str | None]:
    """The expected status and error shape for the rejection: the plan's own field, else read from Expected."""
    sig = case.get("rejection_vs_not_found") if isinstance(case.get("rejection_vs_not_found"), dict) else {}
    rej = sig.get("rejected") if isinstance(sig.get("rejected"), dict) else {}
    status, shape = _text(rej.get("status")).strip() or None, _text(rej.get("error_shape")).strip() or None
    exp = _text(case.get("expected"))
    if not status:
        err = _TRPC_ERR_RE.search(exp)
        codes = _STATUS_RE.findall(exp)
        status = next((c for c in codes if c.startswith("4")), codes[0] if codes else None) or (
            _TRPC_STATUS.get(err.group(1)) if err else None)
        if err and not shape:
            shape = f"error code {err.group(1)}"
    return status, shape


def _not_found_sig(case: dict, framework: str) -> tuple[str, str]:
    sig = case.get("rejection_vs_not_found") if isinstance(case.get("rejection_vs_not_found"), dict) else {}
    nf = sig.get("not_found") if isinstance(sig.get("not_found"), dict) else {}
    if _text(nf.get("status")).strip():
        return _text(nf.get("status")).strip(), _text(nf.get("error_shape")).strip() or "the router's not-found body"
    if framework in mount_tracer.NOT_FOUND:
        return mount_tracer.NOT_FOUND[framework]
    return "404 (or 405)", "the router's own not-found body, not the check's error"


async def check_endpoint_paths(test_plan: Any, report: dict, *, index=None, github=None) -> int:
    """Rule AI over every case section. Returns the number of cases changed."""
    from .deployed_env_checks import ALL_CASE_SECTIONS, gateway_prefixes

    n = 0
    cache: dict = {}
    unfinished: list[str] = []
    prefixes = gateway_prefixes(test_plan)
    for section, i, case in _cases(test_plan, ALL_CASE_SECTIONS):
        eps = _endpoints(case)
        if not eps:
            continue
        entries = _route_entries(case)
        changed = False
        req = case.get("request") if isinstance(case.get("request"), dict) else {}
        negative = bool(_NEGATIVE_RE.search(" ".join([_text(case.get("title")), _text(case.get("expected")),
                                                      _text(req.get("auth"))])))
        for ep in eps:
            entry = _entry_for(ep, entries)
            if entry is None and ep["kind"] == "http":
                continue  # rule AA marks an HTTP call no route_sources entry backs
            if entry is None:
                # The plan cites no definition: find it among the files read, and cite it.
                found = await _find_procedure(index, github, [p for _, p, c in ep["mentions"] if not c], ep["leaf"])
                if found:
                    longest = max((p for _, p, c in ep["mentions"] if not c), key=len, default=ep["leaf"])
                    entry = {"method": "POST", "path": longest, "source": found, "found_by": "rule AI"}
                    case.setdefault("route_sources", []).append(entry)
            trace = await _trace(ep, entry, index, github, cache)
            chain = _model_chain(entry) if entry else []
            model_full = _text(entry.get("path")).strip() if entry and chain else None
            if model_full and ep["kind"] == "http" and not model_full.startswith("/"):
                model_full = None
            chain_bad = _chain_problem(chain, index) if chain else None
            disputed: str | None = None
            full: str | None = None
            why_unverified: str | None = None
            written = [w for w, _, client in ep["mentions"] if not client]
            if trace is not None and trace.full_path:
                full = trace.full_path
                if model_full and _relation(model_full, full, ep["kind"], prefixes) == "other":
                    disputed = f"the plan traces {model_full}, the code traces {full} ({trace.chain_text()})"
            elif model_full and not chain_bad and _is_full(model_full, ep["kind"]):
                full = model_full
                if trace is not None and trace.missing:
                    unfinished.append(f"{model_full}: {trace.missing}")
            else:
                parts = written or [ep["leaf"]]
                why_unverified = (chain_bad or (trace.missing if trace is not None and trace.missing else None)
                                  or _text((entry or {}).get("unverified")).strip()
                                  or ("no route_sources entry traces it" if entry is None else
                                      f"only the router that defines it is cited ({_text(entry.get('source'))}), "
                                      "not the routers and prefix it is mounted under"))
                why_unverified = f"{parts[0]}: {why_unverified}"
            caller = None
            if full and entry is not None and not disputed:
                problem, caller = _caller_problem(entry, full, ep["kind"], index)
                disputed = problem or disputed
            if (disputed or why_unverified) and negative:
                # Whatever the path turns out to be, the tester must not grade a 404 as the rejection.
                fw = _framework(ep, trace, full)
                changed |= _append(case, "how_to_verify", NOT_FOUND_NOTE.format(status=_not_found_sig(case, fw)[0],
                                                                                shape=_not_found_sig(case, fw)[1]))
            if disputed:
                if not _has_marker(case):
                    _mark(case, DISPUTED_MARKER.format(why=disputed), (written or [ep["leaf"]])[0])
                    _record(report, case, section, "AI", "downgraded", disputed[:300], (written or [ep["leaf"]])[0])
                    changed = True
                continue
            if why_unverified:
                if not _has_marker(case):
                    _mark(case, UNVERIFIED_MARKER.format(why=why_unverified), (written or [ep["leaf"]])[0])
                    _record(report, case, section, "AI", "downgraded", why_unverified[:300],
                            (written or [ep["leaf"]])[0])
                    changed = True
                continue
            # Traced: a name that is neither the full path nor a tail of it is disputed.
            other = [w for w in dict.fromkeys(written) if _relation(w, full, ep["kind"], prefixes) == "other"]
            if other:
                why = f"the case calls {other[0]}, the traced path is {full}"
                if not _has_marker(case):
                    _mark(case, DISPUTED_MARKER.format(why=why), other[0])
                    _record(report, case, section, "AI", "downgraded", why[:300], other[0])
                    changed = True
                continue
            # Name the full path everywhere and cite the chain.
            rewritten = []
            for w in dict.fromkeys(written):
                if _relation(w, full, ep["kind"], prefixes) == "partial" and _rewrite(case, w, full):
                    rewritten.append(w)
            if entry is not None:
                if trace is not None and trace.full_path and not _model_chain(entry):
                    entry["mount_chain"] = [{"key": l.key, "source": l.source} for l in trace.links[1:]]
                entry["path"] = full
            chain_text = (trace.chain_text() if trace is not None and trace.full_path else
                          ", ".join(f"mounted as '{_text(c.get('key'))}' at {_text(c.get('source'))}" if _text(c.get("key"))
                                    else f"mounted at {_text(c.get('source'))}" for c in chain))
            if entry is not None and not (trace is not None and trace.full_path):
                chain_text = f"defined at {_text(entry.get('source'))}, " + chain_text
            _append(case, "how_to_verify", TRACED_NOTE.format(path=full, chain=chain_text))
            if caller:
                _append(case, "how_to_verify", CALLER_NOTE.format(caller=caller))
            if rewritten:
                why = (f"named {', '.join(repr(w) for w in rewritten)}; the callable path is {full} "
                       f"({chain_text})")
                _record(report, case, section, "AI", "fixed", why[:300], rewritten[0])
                changed = True
            guards = _guards(entry, trace)
            if guards and (negative or _text(case.get("persona")) or isinstance(case.get("request"), dict)):
                blob = " ".join(_texts(case) + [_text(case.get("preconditions"))]).lower()
                if any(g.split(" ")[0].lower() not in blob for g in guards):
                    _append(case, "preconditions", GUARD_NOTE.format(guards="; ".join(guards)))
                    _record(report, case, section, "AI", "fixed", f"guard carried over from the mount chain: "
                                                                  f"{'; '.join(guards)}"[:300])
                    changed = True
            if negative:
                changed |= _check_negative(case, section, ep, trace, full, report)
        n += changed
    if unfinished:
        report.setdefault("not_run", []).append(
            "the code trace could not confirm the plan's mount chain for " + "; ".join(unfinished[:3])
            + " (the plan's cited chain was kept)")
    return n


def _check_negative(case: dict, section: str, ep: dict, trace, full: str, report: dict) -> bool:
    fw = _framework(ep, trace, full)
    nf_status, nf_shape = _not_found_sig(case, fw)
    status, shape = _rejection(case, ep)
    changed = _append(case, "how_to_verify", NOT_FOUND_NOTE.format(status=nf_status, shape=nf_shape))
    if status:
        changed |= _append(case, "how_to_verify", REJECT_NOTE.format(status=status, shape=shape or "the guard's error"))
    why = None
    if not status:
        why = NO_SIGNATURE_WHY
    elif nf_status.startswith(status) and (not shape or shape.lower() in nf_shape.lower()):
        why = (f"the rejection ({status}) and the route not being found ({nf_status}) look the same; name the "
               f"error body that tells them apart")
    if why and "(rule AI)" not in _text(case.get("expected")):
        case["expected"] = f"Cannot tell rejection from route-not-found (rule AI): {why} — {_text(case.get('expected'))}".strip()
        case["expected_verified"] = False
        _record(report, case, section, "AI", "downgraded", why[:300], full)
        return True
    return changed
