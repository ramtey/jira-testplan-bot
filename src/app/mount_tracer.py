"""Trace an endpoint from where it is defined up to the server entry (rule AI).

A plan named a tRPC procedure ``reports.exportBundle`` from the router file
that defines it. The callable path was ``/trpc/admin.reports.exportBundle``:
the procedure's router is mounted as ``reports`` in an admin router, that one
as ``admin`` in the root router, and the server serves the root router under
``/trpc``. The tester got "No procedure found on path", which looks like the
endpoint failing, or like a no-token check passing for the wrong reason.

This module reads the code to find that chain, so the re-check does not have
to trust the generator's. It is deliberately narrow and never guesses:

* **tRPC** — ``key: childRouter`` inside a ``router(...)``/``createTRPCRouter(...)``
  call (and ``mergeRouters``), climbing to the file that hands the root router
  to an adapter (``fastifyTRPCPlugin`` + ``prefix``, ``createExpressMiddleware``
  under ``app.use('/x', …)``, ``fetchRequestHandler({ endpoint })``, a Next.js
  ``api/trpc/[trpc]`` file).
* **Express / Fastify / Hono / Koa-style routers** — ``router.post('/x')`` in a
  router mounted with ``app.use('/prefix', …, router)``, ``app.route('/prefix',
  router)`` or ``register(plugin, { prefix: '/prefix' })``, up to a file that
  mounts nothing above it.
* **Next.js file routes** — ``app/**/route.ts`` and ``pages/api/**``; the path
  is the file's path (plus ``basePath`` when ``next.config`` sets one).

Parents are looked for only in the files of the definition's ancestor
directories (where routers are mounted in practice), read at the PR ref
through the same index the grounding re-read uses. Two parents, no parent, or
a file that could not be read all end the trace with ``missing`` set: the
caller then marks the path unverified instead of presenting a guess.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
MAX_LEVELS = 8
MAX_FILES_PER_DIR = 40
MAX_FETCHED = 120
_PREFERRED = ("index", "root", "router", "routers", "_app", "app", "server", "main", "api", "trpc", "routes")

# Not-found responses per framework, unless the app sets its own handler (rule AI, item 5).
NOT_FOUND = {
    "trpc": ("404", "tRPC error with data.code \"NOT_FOUND\" (JSON-RPC code -32004) and message "
                    "'No procedure found on path \"<path>\"'"),
    "express": ("404", "Express's default HTML page \"Cannot <METHOD> <path>\""),
    "fastify": ("404", "JSON {\"message\": \"Route <METHOD>:<path> not found\", \"error\": \"Not Found\", "
                       "\"statusCode\": 404}"),
    "hono": ("404", "plain text \"404 Not Found\""),
    "koa": ("404", "plain text \"Not Found\""),
    "nextjs": ("404 (405 for a method the route file does not export)", "Next.js's not-found page, or an empty 405"),
}


@dataclass
class Link:
    key: str          # router key ("gfeDebug"), path prefix ("/api/v2") or "" for a merge
    source: str       # "<path>:<line>"
    kind: str         # "definition" | "router_key" | "path_prefix" | "url_prefix" | "file_route"
    guards: list[str] = field(default_factory=list)


@dataclass
class Trace:
    framework: str
    full_path: str | None = None
    method: str | None = None
    links: list[Link] = field(default_factory=list)
    missing: str | None = None

    @property
    def guards(self) -> list[tuple[str, str]]:
        return [(g, l.source) for l in self.links for g in l.guards]

    def chain_text(self) -> str:
        parts = []
        for l in self.links:
            if l.kind == "definition":
                parts.append(f"defined at {l.source}")
            elif l.kind == "file_route":
                parts.append(f"file route {l.source}")
            elif l.kind == "url_prefix":
                parts.append(f"served under '{l.key or '/'}' at {l.source}")
            elif l.key:
                parts.append(f"mounted as '{l.key}' at {l.source}")
            else:
                parts.append(f"merged into its parent at {l.source}")
        return ", ".join(parts)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _stem(path: str) -> str:
    base = posixpath.basename(path)
    for ext in CODE_EXT:
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _resolves_to(parent_path: str, spec: str, child_path: str) -> bool:
    child_stem = _stem(child_path)
    child_dir = posixpath.dirname(child_path)
    ids = {posixpath.join(child_dir, child_stem)} | ({child_dir} if child_stem == "index" else set())
    if spec.startswith("."):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(parent_path), spec))
    else:
        resolved = re.sub(r"^[~@#]/?", "", spec)
    resolved = re.sub(r"\.(?:[cm]?[jt]sx?)$", "", resolved)
    return any(i == resolved or (not spec.startswith(".") and i.endswith("/" + resolved)) for i in ids)


def _reexports(parent_path: str, parent_text: str, child_path: str, names: set[str]) -> set[str]:
    """Names under which a barrel file (``export { x } from './child'``,
    ``export * from './child'``) passes on one of ``names``."""
    out: set[str] = set()
    for m in re.finditer(r"export\s+(\*|\{[^}]*\})\s*from\s*['\"]([^'\"]+)['\"]", parent_text):
        if not _resolves_to(parent_path, m.group(2), child_path):
            continue
        if m.group(1) == "*":
            out |= {n for n in names if n != "default"}
            continue
        for part in m.group(1).strip("{} ").split(","):
            name, _, alias = part.strip().partition(" as ")
            if name.strip() in names:
                out.add((alias or name).strip())
    # import { x } from './child' … export { x }
    for local, imported in _imports_child(parent_path, parent_text, child_path):
        if imported not in names:
            continue
        for m in re.finditer(r"export\s*\{([^}]*)\}(?!\s*from)", parent_text):
            for part in m.group(1).split(","):
                name, _, alias = part.strip().partition(" as ")
                if name.strip() == local:
                    out.add((alias or name).strip())
    return out


def _imports_child(parent_path: str, parent_text: str, child_path: str) -> list[tuple[str, str]]:
    """``(local name, imported name)`` for each binding ``parent`` imports from ``child``.
    ``imported`` is ``default`` for a default import."""
    child_stem = _stem(child_path)
    child_dir = posixpath.dirname(child_path)
    out = []
    for m in re.finditer(r"import\s+(?:type\s+)?([^;]*?)\s+from\s+['\"]([^'\"]+)['\"]", parent_text):
        if not _resolves_to(parent_path, m.group(2), child_path):
            continue
        clause = m.group(1)
        default = re.match(r"\s*([A-Za-z_$][\w$]*)\s*(?:,|$)", clause)
        if default and not clause.strip().startswith("{") and not clause.strip().startswith("*"):
            out.append((default.group(1), "default"))
        braces = re.search(r"\{([^}]*)\}", clause)
        if braces:
            for part in braces.group(1).split(","):
                part = part.strip()
                if not part or part.startswith("type "):
                    continue
                name, _, alias = part.partition(" as ")
                out.append(((alias or name).strip(), name.strip()))
    for m in re.finditer(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)", parent_text):
        if _stem(m.group(2)) == child_stem or (child_stem == "index" and posixpath.basename(m.group(2)) ==
                                               posixpath.basename(child_dir)):
            out.append((m.group(1), "default"))
    return out


def _exported_as(text: str, var: str) -> set[str]:
    """Names a parent can import ``var`` under: itself, and ``default`` when it is the default export."""
    if var == "<default>":
        return {"default"}
    names = {var}
    v = re.escape(var)
    if re.search(rf"export\s+default\s+(?:async\s+)?(?:function\s*\*?\s*)?{v}\b|module\.exports\s*=\s*{v}\b|"
                 rf"export\s*\{{[^}}]*\b{v}\s+as\s+default\b", text):
        names.add("default")
    return names


# ---------------------------------------------------------------------------
# Workspace: the index plus bounded reads of ancestor directories
# ---------------------------------------------------------------------------

class _Files:
    def __init__(self, index, github, repo: str) -> None:
        self.index, self.github, self.repo = index, github, repo
        self.fetched = 0

    def text(self, path: str) -> str | None:
        got = self.index.files.get((self.repo, path))
        return got.text if got is not None else None

    async def dir_files(self, directory: str, exclude: set[str]) -> tuple[list[tuple[str, str]], list[str]]:
        """Code files directly in ``directory``: ``([(path, text)], [unreadable paths])``."""
        tree = self.index.trees.get(self.repo) or []
        names = [p for p in tree if posixpath.dirname(p) == directory and p.endswith(CODE_EXT)
                 and p not in exclude and not re.search(r"\.(?:test|spec|d)\.[cm]?[jt]sx?$", p)]
        names.sort(key=lambda p: (_stem(p) not in _PREFERRED, p))
        names = names[:MAX_FILES_PER_DIR]
        want = [p for p in names if (self.repo, p) not in self.index.files]
        if want and self.github is not None and self.fetched < MAX_FETCHED:
            want = want[: MAX_FETCHED - self.fetched]
            self.fetched += len(want)
            try:
                got = await self.github.fetch_files_at_ref(self.repo, self.index.refs.get(self.repo), want) or {}
            except Exception as e:  # noqa: BLE001 — unread stays unread
                logger.warning("mount trace could not read %s: %s", directory, e)
                got = {}
            from .source_facts import SourceFile

            for p, t in got.items():
                self.index.files[(self.repo, p)] = SourceFile(self.repo, p, self.index.refs.get(self.repo), t)
        out, unread = [], []
        for p in names:
            t = self.text(p)
            (out.append((p, t)) if t is not None else unread.append(p))
        return out, unread


# Test files build throwaway apps; never a mount of the real one.
_TEST_FILE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|e2e|fixtures?|mocks?)/|\.(?:test|spec)\.[cm]?[jt]sx?$", re.I)


def _ancestors(path: str, stop_at: str = "") -> list[str]:
    out = []
    d = posixpath.dirname(path)
    while True:
        out.append(d)
        if not d or d == stop_at:
            break
        d = posixpath.dirname(d)
    return out


def _package_root(path: str, tree: list[str]) -> str:
    """The nearest ancestor holding a package.json, so the climb stays inside one app."""
    pkgs = {posixpath.dirname(p) for p in tree if posixpath.basename(p) == "package.json"}
    for d in _ancestors(path):
        if d in pkgs:
            return d
    return ""


# ---------------------------------------------------------------------------
# tRPC
# ---------------------------------------------------------------------------

_TRPC_ROUTER_CALL = r"(?:createTRPCRouter|createRouter|t\.router|router|createTRPCRouterWithMiddleware)\s*\("
_TRPC_ADAPTERS = re.compile(
    r"fastifyTRPCPlugin|createExpressMiddleware|createHTTPServer|createHTTPHandler|fetchRequestHandler|"
    r"createNextApiHandler|trpcServer|createAWSLambdaHandler|nodeHTTPRequestHandler|createOpenApi\w*Handler")


def _enclosing_const(text: str, pos: int, call: str) -> tuple[str, int] | None:
    """The ``const X = <call>(`` whose call encloses ``pos``."""
    best = None
    for m in re.finditer(rf"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]{{0,120}})?=\s*{call}",
                         text):
        if m.start() > pos:
            break
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        if i >= pos:
            best = (m.group(1), m.start())
    return best


def _enclosing_record(text: str, pos: int) -> tuple[str, int] | None:
    """A router written as a plain object: ``const x = { … } satisfies TRPCRouterRecord``
    or ``const x: TRPCRouterRecord = { … }`` (create-t3-app v7 style)."""
    best = None
    for m in re.finditer(r"(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(:\s*TRPCRouterRecord\s*)?=\s*\{", text):
        if m.start() > pos:
            break
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        if i >= pos and (m.group(2) or re.match(r"\s*satisfies\s+TRPCRouterRecord\b", text[i: i + 60])):
            best = (m.group(1), m.start())
    return best


def enclosing_router(text: str, pos: int) -> tuple[str, int] | None:
    """The tRPC router (call or plain record) whose body holds ``pos``; the innermost one."""
    found = [r for r in (_enclosing_const(text, pos, _TRPC_ROUTER_CALL), _enclosing_record(text, pos)) if r]
    return max(found, key=lambda r: r[1]) if found else None


def is_trpc(text: str) -> bool:
    return bool(re.search(r"@trpc/server|TRPCRouterRecord|\b(?:public|protected|private|admin)\w*Procedure\b|" + _TRPC_ROUTER_CALL, text))


def _procedure(text: str, name: str) -> tuple[int, list[str], int] | None:
    """``(line, guards, offset)`` of the procedure ``name`` defined inside a router body."""
    ms = list(re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*:\s*([A-Za-z_$][\w$]*)", text))
    m = next((x for x in ms if enclosing_router(text, x.start())), None)
    if not m:
        return None
    # Guards: the procedure builder and its .use(...) middlewares, up to .query/.mutation/.subscription.
    tail = text[m.start(1): m.start(1) + 600]
    end = re.search(r"\.(?:query|mutation|subscription)\s*\(", tail)
    builder = tail[: end.start()] if end else tail[:200]
    builder_name = m.group(1)
    guards = [builder_name] if (re.search(r"Procedure$", builder_name) and not builder_name.startswith("public")) or \
        re.search(r"require|admin|auth|protected|guard|role|permission", builder_name, re.I) else []
    guards += re.findall(r"\.use\(\s*([A-Za-z_$][\w$]*)", builder)
    return _line_of(text, m.start()), guards, m.start()


def _mounts_of(text: str, var_names: set[str]) -> list[tuple[str, int, str]]:
    """``(key, line, parent router var)`` for each place ``text`` mounts one of ``var_names``."""
    out = []
    for v in var_names:
        for m in re.finditer(rf"(?:([A-Za-z_$][\w$]*|['\"][^'\"]+['\"])\s*:\s*)?(?<![\w$.]){re.escape(v)}\b(?!\s*[:(.])",
                             text):
            parent = enclosing_router(text, m.start())
            if parent is None:
                parent = _enclosing_const(text, m.start(), r"(?:t\.)?mergeRouters\s*\(")
                if parent is None:
                    continue
                out.append(("", _line_of(text, m.start()), parent[0]))
                continue
            if parent[0] == v:
                continue
            key = (m.group(1) or v).strip("'\"")
            # `key: value` inside an import/destructure is not a mount.
            line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end())]
            if re.match(r"\s*import\b", line):
                continue
            out.append((key, _line_of(text, m.start()), parent[0]))
    return list(dict.fromkeys(out))


def _trpc_prefix(path: str, text: str, root_names: set[str]) -> tuple[str, int] | None:
    """The URL prefix under which this file serves one of ``root_names``, if it does."""
    nm = re.search(r"(?:^|/)pages/api/(.+)/\[(?:\.\.\.)?\w+\]\.[cm]?[jt]sx?$|(?:^|/)app/(.+)/\[(?:\.\.\.)?\w+\]/route\.[cm]?[jt]sx?$",
                   path)
    for m in _TRPC_ADAPTERS.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if re.match(r"\s*(?:import|export\s*\{)", text[line_start:m.start()] or text[line_start:line_start + 10]):
            continue
        window = text[m.start(): m.start() + 800]
        if not any(re.search(rf"\brouter\s*:\s*{re.escape(n)}\b|\brouter\s*[,}}]", window) and
                   (re.search(rf"\b{re.escape(n)}\b", window) or "router" in window) for n in root_names):
            continue
        line = _line_of(text, m.start())
        pm = re.search(r"\b(?:prefix|endpoint)\s*:\s*['\"`]([^'\"`]+)['\"`]", window)
        if pm:
            return pm.group(1), line
        before = text[max(0, m.start() - 200): m.start()]
        um = re.search(r"\.use\(\s*['\"`]([^'\"`]+)['\"`]\s*,\s*[\w.]*$", before)
        if um:
            return um.group(1), _line_of(text, m.start() - 200 + um.start())
        if nm:
            seg = "api/" + nm.group(1) if nm.group(1) else nm.group(2)
            return "/" + seg.strip("/"), line
        if re.search(r"createHTTPServer|createHTTPHandler", m.group(0)):
            return "", line
    return None


async def trace_trpc(files: _Files, def_path: str, proc: str) -> Trace:
    t = Trace("trpc")
    text = files.text(def_path)
    if text is None:
        t.missing = f"{def_path} could not be read"
        return t
    found = _procedure(text, proc)
    if not found:
        t.missing = f"procedure '{proc}' is not defined in {def_path}"
        return t
    line, guards, _ = found
    t.links.append(Link(proc, f"{def_path}:{line}", "definition", guards))
    router = enclosing_router(text, found[2])
    if router is None:
        t.missing = f"no router(...) encloses '{proc}' in {def_path}"
        return t
    var, cur_path, cur_text = router[0], def_path, text
    keys: list[str] = []
    tree = files.index.trees.get(files.repo) or []
    stop = _package_root(def_path, tree)
    for _ in range(MAX_LEVELS):
        names = _exported_as(cur_text, var)
        # Same file first (a root router can sit beside its children, or the server can serve it).
        pref = _trpc_prefix(cur_path, cur_text, {var})
        if pref is not None:
            t.links.append(Link(pref[0], f"{cur_path}:{pref[1]}", "url_prefix"))
            t.full_path = _join_trpc(pref[0], keys[::-1] + [proc])
            return t
        local = [x for x in _mounts_of(cur_text, {var})]
        parents: list[tuple[str, str, str, int, str]] = []  # (path, text, key, line, parent var)
        for key, ln, pvar in local:
            parents.append((cur_path, cur_text, key, ln, pvar))
        unread: list[str] = []
        barrels: list[tuple[str, str, set[str]]] = []
        if not parents:
            for d in _ancestors(cur_path, stop):
                got, miss = await files.dir_files(d, {cur_path})
                unread += miss
                for p, ptext in got:
                    passed = _reexports(p, ptext, cur_path, names)
                    if passed:
                        barrels.append((p, ptext, passed))
                    imported = [loc for loc, imp in _imports_child(p, ptext, cur_path) if imp in names]
                    if not imported:
                        continue
                    pref = _trpc_prefix(p, ptext, set(imported))
                    if pref is not None:
                        t.links.append(Link(pref[0], f"{p}:{pref[1]}", "url_prefix"))
                        t.full_path = _join_trpc(pref[0], keys[::-1] + [proc])
                        return t
                    for key, ln, pvar in _mounts_of(ptext, set(imported)):
                        parents.append((p, ptext, key, ln, pvar))
                if parents:
                    break
        if not parents and len(barrels) == 1:
            # Re-exported unchanged: climb on from the barrel, same router, no new key.
            cur_path, cur_text = barrels[0][0], barrels[0][1]
            var = next(iter(barrels[0][2]))
            continue
        uniq = {(p, k, pv) for p, _, k, _, pv in parents}
        if len(uniq) > 1:
            where = "; ".join(f"{p}:{ln} as '{k}'" for p, _, k, ln, _ in parents[:3])
            t.missing = f"router '{var}' is mounted in more than one place ({where})"
            return t
        if not parents:
            t.missing = (f"no file mounts '{var}' ({cur_path}) or serves it under a URL prefix"
                         + (f"; {len(unread)} candidate file(s) could not be read" if unread else ""))
            return t
        p, ptext, key, ln, pvar = parents[0]
        t.links.append(Link(key, f"{p}:{ln}", "router_key"))
        if key:
            keys.append(key)
        var, cur_path, cur_text = pvar, p, ptext
    t.missing = f"gave up after {MAX_LEVELS} router levels"
    return t


def _join_trpc(prefix: str, keys: list[str]) -> str:
    proc = ".".join(k for k in keys if k)
    prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
    return f"{prefix}/{proc}"


# ---------------------------------------------------------------------------
# Express / Fastify / Hono / Koa-style routers
# ---------------------------------------------------------------------------

_VERB = r"get|post|put|patch|delete|del|head|options|all"


_Q = r"['\"`]"
_UNFOLLOWED = re.compile(r"\.basePath\s*\(|setGlobalPrefix\s*\(|@fastify/autoload|\bAutoLoad\b|\brewriteUrl\b|"
                         r"\bnew\s+App\s*\(\s*\[|routes\.forEach|\.map\(\s*\(?\s*route\b")


def _class_path(text: str) -> str | None:
    """``public path = '/envelopes'`` of a class-based router."""
    m = re.search(r"\b(?:public\s+|private\s+|readonly\s+)*path\s*(?::\s*string\s*)?=\s*['\"`]([^'\"`]*)['\"`]", text)
    return m.group(1) if m else None


def _http_definition(text: str, sub_path: str | None, method: str | None) -> tuple[str, str, int, str] | None:
    """``(router var, registered path, line, method)`` for the route ``sub_path``
    (or the only route, when ``sub_path`` is None)."""
    hits = []
    cls = _class_path(text)
    recv = r"((?:this\.)?[A-Za-z_$][\w$]*)(?:\s*\.\s*withTypeProvider\s*<[^>]*>\s*\(\s*\))?"
    for m in re.finditer(rf"\b{recv}\s*\.\s*({_VERB})\s*(?:<[^()]*?>)?\s*\(\s*{_Q}([^'\"`]*){_Q}", text, re.I):
        path = m.group(3).replace("${this.path}", cls) if cls is not None else m.group(3)
        if "${" in path:
            continue
        hits.append((m.group(1), path, _line_of(text, m.start()), m.group(2).upper().replace("DEL", "DELETE")))
    for m in re.finditer(rf"\b{recv}\s*\.\s*route\s*\(\s*\{{", text):
        window = text[m.end(): m.end() + 2500]
        mm = re.search(rf"\bmethod\s*:\s*\[?\s*{_Q}(\w+){_Q}", window)
        um = re.search(rf"\burl\s*:\s*{_Q}([^'\"`]*){_Q}", window)
        if mm and um:
            hits.append((m.group(1), um.group(1), _line_of(text, m.start()), mm.group(1).upper()))
    if method:
        hits = [h for h in hits if h[3] in (method.upper(), "ALL")] or hits
    if sub_path is not None:
        from .deployed_env_checks import route_matches

        # A catch-all ('*', '/:any') is not the definition of a named path.
        statics = lambda p: [x for x in p.split("/") if x and not x.startswith((":", "*", "{", "[")) and x != "(.*)"]
        exact = [h for h in hits if route_matches(h[1] or "/", sub_path)
                 and (statics(h[1]) or not statics(sub_path))
                 # The plugin root is only the route registered at '' or '/'.
                 and (sub_path.strip("/") or not (h[1] or "/").strip("/"))]
        return exact[0] if len(exact) == 1 else None
    return hits[0] if len(hits) == 1 else None


def _http_mounts(text: str, names: set[str]) -> list[tuple[str, int, str, list[str]]]:
    """``(prefix, line, parent var, middlewares)`` for each mount of one of ``names``."""
    out = []
    for n in names:
        en = re.escape(n)
        for m in re.finditer(rf"\b([A-Za-z_$][\w$]*)\s*\.\s*(?:use|route)\s*\(\s*['\"`]([^'\"`]*)['\"`]\s*,([^;]*?)\b{en}\b"
                             r"(?:\.routes\(\))?\s*\)", text):
            mws = [x.strip() for x in m.group(3).split(",") if re.fullmatch(r"\s*[A-Za-z_$][\w$.]*(?:\([^)]*\))?\s*", x)]
            out.append((m.group(2), _line_of(text, m.start()), m.group(1), [w for w in mws if w]))
        for m in re.finditer(rf"\b([A-Za-z_$][\w$]*)\s*\.\s*register\s*(\()\s*{en}\b", text):
            args = _split_args(text, m.start(2))
            out.append((_options_prefix(args[1] if len(args) > 1 else None), _line_of(text, m.start()), m.group(1), []))
        for m in re.finditer(rf"\b([A-Za-z_$][\w$]*)\s*\.\s*use\s*\(\s*{en}\b(?:\.routes\(\))?\s*\)", text):
            out.append(("", _line_of(text, m.start()), m.group(1), []))
        out += [(pre, ln, pv, []) for pre, ln, pv in _call_mounts(text, n)]
    return list({(a, b, c): (a, b, c, d) for a, b, c, d in out}.values())


def _call_mounts(text: str, fn: str) -> list[tuple[str, int, str]]:
    """``(prefix, line, app var)`` for each statement that calls ``fn(app)``.

    A bootstrap like ``server.ts`` often hands the app to a registration
    function (``await registerRoutes(app)``) that does the ``register(…,
    { prefix })`` calls itself. That call is the mount: it adds no prefix of
    its own, and the routes are served on whatever ``app`` is. Only a call
    standing as its own statement counts, with a plain identifier as its
    first argument — not the definition, an import, or ``x.fn(app)``.
    """
    out = []
    for m in re.finditer(rf"(?:^|[;{{}}]|\bawait|\bvoid|\breturn)\s*(?<![\w$.]){re.escape(fn)}\s*\(\s*"
                         rf"([A-Za-z_$][\w$]*)\s*[,)]", text, re.M):
        line_start = text.rfind("\n", 0, m.start(1)) + 1
        line = text[line_start: text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)]
        if re.match(r"\s*(?:import|export\s*\{)", line) or re.search(
                rf"\bfunction\s*\*?\s*{re.escape(fn)}\s*\(", line):
            continue
        out.append(("", _line_of(text, m.start(1)), m.group(1)))
    return out


DEFAULT = "<default>"


def _router_var_of(text: str, var: str) -> str:
    """For a Fastify-style plugin ``function routes(fastify)``, the plugin's name
    (``DEFAULT`` for an anonymous default export)."""
    v = re.escape(var.replace("this.", ""))
    m = re.search(rf"(?:export\s+default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(\s*{v}\b", text)
    if m:
        return m.group(1)
    m = re.search(rf"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]*)?=\s*(?:async\s+)?(?:function\s*\w*\s*\(\s*{v}\b|"
                  rf"\(\s*{v}\b[^)]*\)\s*=>)", text)
    if m:
        return m.group(1)
    if re.search(rf"(?:export\s+default|module\.exports\s*=)\s*(?:async\s+)?(?:function\s*\(\s*{v}\b|\(\s*{v}\b[^)]*\)\s*=>)",
                 text):
        return DEFAULT
    return var


UNKNOWN_PREFIX = "<unknown>"


def _split_args(text: str, open_idx: int) -> list[str]:
    """The top-level arguments of the call whose ``(`` is at ``open_idx``."""
    args, depth, quote, start, i = [], 0, "", open_idx + 1, open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append(text[start:i])
                return [x.strip() for x in args]
        elif ch == "," and depth == 1:
            args.append(text[start:i])
            start = i + 1
        i += 1
    return [x.strip() for x in args]


def _options_prefix(opts: str | None) -> str:
    """The ``prefix`` a ``register`` options argument sets; UNKNOWN_PREFIX when it cannot be read."""
    if not opts:
        return ""
    if not opts.startswith("{"):
        return UNKNOWN_PREFIX
    m = re.search(r"\bprefix\s*:\s*(\S[^,}]*)", opts)
    if not m:
        return UNKNOWN_PREFIX if "..." in opts else ""
    lit = re.fullmatch(rf"{_Q}([^'\"`$]*){_Q}", m.group(1).strip())
    return lit.group(1) if lit else UNKNOWN_PREFIX


def _inline_plugin(text: str, var: str) -> tuple[str, str, int] | None:
    """``(outer var, prefix, line)`` when ``var`` is the parameter of a function
    passed inline to ``outer.register(fn, { prefix })``."""
    m = re.search(rf"\b([A-Za-z_$][\w$]*)\s*\.\s*register\s*(\()\s*(?:async\s+)?(?:function\s*\w*\s*)?\(\s*{re.escape(var)}\b",
                  text)
    if not m:
        return None
    args = _split_args(text, m.start(2))
    return m.group(1), _options_prefix(args[1] if len(args) > 1 else None), _line_of(text, m.start())


def _is_entry(text: str, var: str) -> bool:
    v = re.escape(var)
    return bool(re.search(rf"\b{v}\s*\.\s*listen\s*\(|\b(?:serve|createServer)\s*\(\s*(?:\{{[^}}]*fetch\s*:\s*)?{v}\b"
                          rf"|\b(?:const|let|var)\s+{v}\s*(?::[^=]+)?=\s*(?:await\s+)?(?:express|Fastify|fastify|new\s+Hono|"
                          rf"new\s+Koa)\s*\(", text))


async def trace_http(files: _Files, def_path: str, sub_path: str | None, method: str | None,
                     framework: str) -> Trace:
    t = Trace(framework)
    text = files.text(def_path)
    if text is None:
        t.missing = f"{def_path} could not be read"
        return t
    d = _http_definition(text, sub_path, method)
    if not d:
        t.missing = (f"no single route registration for {sub_path} in {def_path}" if sub_path
                     else f"no single route registration in {def_path}")
        return t
    var, reg, line, meth = d
    t.method = meth
    t.links.append(Link(reg, f"{def_path}:{line}", "definition"))
    prefixes = [reg]
    if var.startswith("this.") or _UNFOLLOWED.search(text):
        t.missing = (f"{def_path} registers routes on a class or in a way this check does not follow; "
                     "cite where it is mounted")
        return t
    plugin = _router_var_of(text, var)
    var, cur_path, cur_text = plugin, def_path, text
    tree = files.index.trees.get(files.repo) or []
    stop = _package_root(def_path, tree)
    for _ in range(MAX_LEVELS):
        names = _exported_as(cur_text, var)
        parents = [(cur_path, cur_text, *x) for x in _http_mounts(cur_text, {var})]
        unread: list[str] = []
        if not parents:
            for dname in _ancestors(cur_path, stop):
                got, miss = await files.dir_files(dname, {cur_path})
                unread += miss
                for p, ptext in got:
                    imported = {loc for loc, imp in _imports_child(p, ptext, cur_path) if imp in names}
                    if imported:
                        parents += [(p, ptext, *x) for x in _http_mounts(ptext, imported)]
                if parents:
                    break
        if not parents:
            # A registration file outside the ancestor directories
            # (src/http/register-routes.ts for src/routes/cron.ts) may already
            # be read: the route re-check reads the entry and what it mounts.
            for (repo, p), f in list(files.index.files.items()):
                if repo != files.repo or p == cur_path or not p.endswith(CODE_EXT) or (
                        stop and not p.startswith(stop + "/")) or _TEST_FILE.search(p):
                    continue
                imported = {loc for loc, imp in _imports_child(p, f.text, cur_path) if imp in names}
                if imported:
                    parents += [(p, f.text, *x) for x in _http_mounts(f.text, imported)]
        if len({(p, pre, pv) for p, _, pre, _, pv, _ in parents}) > 1:
            where = "; ".join(f"{p}:{ln} under '{pre}'" for p, _, pre, ln, _, _ in parents[:3])
            t.missing = f"'{var}' is mounted in more than one place ({where})"
            return t
        if not parents:
            if _is_entry(cur_text, var) or cur_path == def_path and _is_entry(cur_text, var):
                t.full_path = _join_http(prefixes)
                return t
            t.missing = (f"no file mounts '{var}' ({cur_path}) and it is not the server entry"
                         + (f"; {len(unread)} candidate file(s) could not be read" if unread else ""))
            return t
        p, ptext, pre, ln, pvar, mws = parents[0]
        if pre == UNKNOWN_PREFIX:
            t.missing = f"the prefix '{var}' is registered under at {p}:{ln} is not a literal"
            return t
        t.links.append(Link(pre, f"{p}:{ln}", "path_prefix", mws))
        prefixes.append(pre)
        if _UNFOLLOWED.search(ptext):
            t.missing = (f"{p} mounts routes in a way this check does not follow "
                         f"({_UNFOLLOWED.search(ptext).group(0).strip()})")
            return t
        inline = _inline_plugin(ptext, pvar)
        while inline is not None:  # register(async (instance) => instance.register(routes), { prefix })
            outer, ipre, iln = inline
            if ipre == UNKNOWN_PREFIX:
                t.missing = f"the prefix of the plugin registered at {p}:{iln} is not a literal"
                return t
            t.links.append(Link(ipre, f"{p}:{iln}", "path_prefix"))
            prefixes.append(ipre)
            pvar = outer
            inline = _inline_plugin(ptext, pvar)
        var, cur_path, cur_text = _router_var_of(ptext, pvar), p, ptext
        if _is_entry(ptext, pvar) and not _http_mounts(ptext, _exported_as(ptext, pvar)):
            t.full_path = _join_http(prefixes)
            return t
    t.missing = f"gave up after {MAX_LEVELS} mount levels"
    return t


def _join_http(prefixes: list[str]) -> str:
    segs = [s for p in reversed(prefixes) for s in p.split("/") if s]
    return "/" + "/".join(segs)


# ---------------------------------------------------------------------------
# Next.js file routes
# ---------------------------------------------------------------------------

_NEXT_APP = re.compile(r"(?:^|/)app/(.*?)/?route\.[cm]?[jt]sx?$")
_NEXT_PAGES = re.compile(r"(?:^|/)pages/(api/.*?)(?:/index)?\.[cm]?[jt]sx?$")


def next_file_route(path: str) -> str | None:
    m = _NEXT_APP.search(path) or _NEXT_PAGES.search(path)
    if not m:
        return None
    segs = []
    for s in m.group(1).split("/"):
        if not s or (s.startswith("(") and s.endswith(")")) or s.startswith("@"):
            continue
        dyn = re.fullmatch(r"\[\[?(?:\.\.\.)?(\w+)\]?\]", s)
        segs.append(f":{dyn.group(1)}" if dyn else s)
    return "/" + "/".join(segs)


async def trace_next(files: _Files, def_path: str) -> Trace:
    t = Trace("nextjs")
    route = next_file_route(def_path)
    if route is None:
        t.missing = f"{def_path} is not a Next.js route file"
        return t
    t.links.append(Link(route, f"{def_path}:1", "file_route"))
    root = re.split(r"(?:^|/)(?:src/)?(?:app|pages)/", def_path)[0]
    cfgs = [posixpath.join(root, n) for n in ("next.config.js", "next.config.mjs", "next.config.ts")]
    tree = set(files.index.trees.get(files.repo) or [])
    present = [c for c in cfgs if c in tree]
    if present:
        got, miss = await files.dir_files(posixpath.dirname(present[0]), set())
        text = dict(got).get(present[0])
        if text is None:
            t.missing = f"{present[0]} could not be read, so basePath is unknown"
            return t
        bm = re.search(r"basePath\s*:\s*['\"`]([^'\"`]*)['\"`]", text)
        if bm and bm.group(1).strip("/"):
            t.links.append(Link(bm.group(1), f"{present[0]}:{_line_of(text, bm.start())}", "url_prefix"))
            route = "/" + bm.group(1).strip("/") + route
    t.full_path = route
    return t


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve(index, locator: str) -> tuple[str, str] | None:
    try:
        keys = index.candidates(locator)
    except Exception:  # noqa: BLE001
        return None
    keys = [k for k in keys if k in index.files] or keys
    return keys[0] if len(keys) == 1 else None


async def trace(index, github, source: str, *, procedure: str | None = None, sub_path: str | None = None,
                method: str | None = None) -> Trace | None:
    """Trace the endpoint whose definition ``source`` cites. None when the
    file's framework is not one this module knows (the caller relies on the
    plan's own chain then)."""
    if index is None or isinstance(index, str) or not source:
        return None
    key = _resolve(index, source)
    if key is None:
        return None
    repo, path = key
    files = _Files(index, github, repo)
    text = files.text(path)
    if text is None:
        return Trace("unknown", missing=f"{path} could not be read")
    if next_file_route(path) and re.search(r"export\s+(?:async\s+)?(?:function|const)\s+(?:GET|POST|PUT|PATCH|DELETE)\b|"
                                           r"export\s+default\s+(?:async\s+)?function", text):
        return await trace_next(files, path)
    if procedure and is_trpc(text):
        return await trace_trpc(files, path, procedure)
    if re.search(rf"\.\s*(?:{_VERB})\s*(?:<[^()]*?>)?\s*\(\s*['\"`][/$]|\.route\s*\(\s*\{{", text, re.I):
        fw = ("fastify" if re.search(r"fastify|FastifyInstance", text, re.I) else
              "hono" if re.search(r"\bHono\b", text) else
              "koa" if re.search(r"koa", text, re.I) else "express")
        return await trace_http(files, path, sub_path, method, fw)
    return None
