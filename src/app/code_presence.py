"""Is a fact the plan states actually in the code? Searched across every file read, not one.

Two checks labelled facts "unverified" or "not checked" that were plainly in the
repository at the plan's commit (SK-3014, plan 734):

* a step querying ``debug_data->'softRefusals'`` was marked "label per ticket,
  not found in code" because the one cited file spells the column
  ``soft_refusals``; another changed file writes ``debug_data.softRefusals`` on
  every turn;
* nine requests to ``POST /api/stream-text/chat/<uuid>`` were marked "Route not
  checked against the router (rule AA)" although ``app.ts`` registers the
  routes plugin with ``prefix: '/api/stream-text'`` and the plugin declares
  ``method: 'POST', url: '/chat/:uuid'``.

This module answers both from what was read at the PRs' head commits
(``SourceIndex``): the changed files read whole, the cited files, the router
files ``read_router_context`` adds, and the PR patches (every file type, so a
SQL migration counts).

``find_identifier``
    An identifier in every syntactic form — a variable, ``.prop`` access, an
    object or JSON key, a string literal. The exact spelling is a hit; a
    different casing (``soft_refusals`` for ``softRefusals``) is reported as a
    variant, never as a hit: in a JSON path or a query the spelling is the fact.

``resolve_route``
    A method and path, composed from the router registration prefix (fastify
    ``register(x, {prefix})``, express/hono ``use``/``route``, FastAPI
    ``include_router(prefix=)``/``APIRouter(prefix=)``, Nest ``@Controller``)
    and the route's own declaration, across files and through nested mounts.
    A declaration whose file no read file mounts counts only when that file
    creates the app itself: otherwise the prefix is unknown, which is reported
    as such.

Both are pure over the index except ``read_router_context``, which reads the
app entry files and router files a called path names, so the route check is
not limited to what the generator chose to cite.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .source_facts import SourceFile, SourceIndex, is_source_path

# --- identifiers --------------------------------------------------------------

#: A label shaped like code (``softRefusals``, ``soft_refusals``,
#: ``debug_data.softRefusals``), not on-screen text ("Save", "Sign In-Person").
_CODE_SHAPED_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|->>?|::)[A-Za-z_$][\w$]*)*$")


def is_code_identifier(text: str) -> bool:
    t = (text or "").strip()
    if not _CODE_SHAPED_RE.match(t):
        return False
    return "_" in t or "." in t or "->" in t or "::" in t or bool(re.search(r"[a-z][A-Z]", t))


def _words(name: str) -> list[str]:
    parts = re.split(r"[_\-\s]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name))
    return [p.lower() for p in parts if p]


def spellings(name: str) -> list[str]:
    """The same identifier in camelCase, PascalCase, snake_case, kebab-case and
    SCREAMING_CASE, exact spelling first."""
    w = _words(name)
    if not w:
        return [name]
    out = [name, w[0] + "".join(x.title() for x in w[1:]), "".join(x.title() for x in w),
           "_".join(w), "-".join(w), "_".join(w).upper()]
    return list(dict.fromkeys(out))


def _token_re(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


@dataclass
class Hit:
    repo: str
    path: str
    line: int
    text: str

    def where(self) -> str:
        return f"{self.path}:{self.line}"


def patch_lines(patch: str | None) -> list[tuple[int, str]]:
    """``(new-file line, text)`` for the context and added lines of a diff."""
    out: list[tuple[int, str]] = []
    n = 0
    for line in (patch or "").splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if not n or line.startswith("\\"):
            continue
        if line.startswith("-"):
            continue
        out.append((n, line[1:] if line[:1] in "+ " else line))
        n += 1
    return out


def _corpus(index: SourceIndex):
    """``(repo, path, line, text)`` over every file read whole, then the patch
    lines of diff files that were not."""
    for (repo, path), src in index.files.items():
        for n, text in enumerate(src.lines, 1):
            yield repo, path, n, text
    for (repo, path), patch in getattr(index, "patches", {}).items():
        if (repo, path) in index.files:
            continue
        for n, text in patch_lines(patch):
            yield repo, path, n, text


def find_identifier(index: SourceIndex | None, name: str) -> tuple[Hit | None, Hit | None]:
    """``(exact hit, variant hit)`` for an identifier anywhere in the index.

    A dotted path (``debug_data.softRefusals``) is found when its last part is
    — that is the key the claim is about."""
    if index is None or not name:
        return None, None
    name = name.strip()
    parts = [p for p in re.split(r"\.|->>?|::", name) if p]
    targets = [name] + ([parts[-1]] if len(parts) > 1 else [])
    exact_res = [_token_re(t) for t in targets]
    variants = [v for t in targets for v in spellings(t)[1:] if v not in targets]
    variant_res = [_token_re(v) for v in dict.fromkeys(variants)]
    variant: Hit | None = None
    # Changed lines first, so the citation points at the code under test.
    ordered = sorted(_corpus(index), key=lambda r: (_unchanged(index, r[0], r[1], r[2]),
                                                    bool(_COMMENT_RE.match(r[3])), r[1], r[2]))
    for repo, path, n, text in ordered:
        if any(rx.search(text) for rx in exact_res):
            return Hit(repo, path, n, text.strip()), None
        if variant is None and any(rx.search(text) for rx in variant_res):
            variant = Hit(repo, path, n, text.strip())
    return None, variant


_COMMENT_RE = re.compile(r"^\s*(?://|#|\*|/\*|--)")


def _unchanged(index: SourceIndex, repo: str, path: str, line: int) -> int:
    return 0 if line in index.changed_lines.get((repo, path), ()) else 1


def searched_scope(index: SourceIndex | None) -> str:
    """Where a search looked, for a warning that keeps the claim unverified."""
    if index is None:
        return "no source was read"
    files = set(index.files) | set(getattr(index, "patches", {}))
    refs = ", ".join(f"{posixpath.basename(r)}@{(ref or '')[:8]}" for r, ref in index.refs.items()) or "the PR head"
    return f"searched {len(files)} file(s) the PRs change or the plan cites, at {refs}"


# --- routes -------------------------------------------------------------------

_TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|e2e|fixtures?|mocks?)/|\.(?:test|spec)\.\w+$|_test\.go$",
                           re.I)
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_Q = r"['\"`]"

_CALL_DECL_RE = re.compile(
    r"\.(get|post|put|patch|delete|head|options|all)\s*(?:<[^>()]*>)?\(\s*" + _Q
    + r"(/[^'\"`]*|\$\{[\w.]+\}[^'\"`]*)" + _Q, re.I)
#: ``Router()`` and friends: a file that builds a router a loop may mount.
_MAKES_ROUTER_RE = re.compile(r"\b(?:express\.)?Router\(\s*\)|\bAPIRouter\(|\bnew\s+Router\(")
_PY_DECL_RE = re.compile(
    r"@[\w.]+\.(get|post|put|patch|delete|head|options|api_route|route)\(\s*['\"]([^'\"]*)['\"]([^)]*)\)", re.I)
_NEST_DECL_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Head|Options|All)\(\s*(?:['\"`]([^'\"`]*)['\"`])?\s*\)")
_NEST_CTRL_RE = re.compile(r"@Controller\(\s*(?:\{[^}]*?path\s*:\s*)?['\"`]([^'\"`]*)['\"`]")
_OBJ_METHOD_RE = re.compile(r"\bmethod\s*:\s*(\[[^\]]*\]|['\"`]\w+['\"`])", re.I)
_OBJ_URL_RE = re.compile(r"\b(?:url|path)\s*:\s*['\"`](/[^'\"`]*)['\"`]")
_PY_ROUTER_PREFIX_RE = re.compile(r"\b(\w+)\s*=\s*APIRouter\([^)]*?prefix\s*=\s*['\"]([^'\"]*)['\"]", re.S)

_FASTIFY_MOUNT_RE = re.compile(
    r"\.register\(\s*([\w$.]+)(?:\(\))?\s*,\s*\{[^{}]*?\bprefix\s*:\s*" + _Q + r"([^'\"`]*)" + _Q, re.S)
_USE_MOUNT_RE = re.compile(
    r"\.(?:use|route)\(\s*" + _Q + r"(/[^'\"`]*)" + _Q + r"\s*,\s*(?:[\w$.]+(?:\([^()]*\))?\s*,\s*)*"
    r"([\w$.]+)(?:\(\))?\s*\)")
# Go 1.22 ServeMux: ``api.HandleFunc("GET /delegations/{id}", h)``, mounted as
# ``mux.Handle("/api/", …http.StripPrefix("/api", …(api)))``.
_GO_DECL_RE = re.compile(r"\b(\w+)\.Handle(?:Func)?\(\s*\"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[^\"]*)\"")
_GO_MOUNT_RE = re.compile(r"\.Handle\(\s*\"(/[^\" ]*)\"\s*,([^\n]*)\)\s*$", re.M)
_PY_MOUNT_RE = re.compile(r"include_router\(\s*([\w.]+)\s*(?:,([^)]*))?\)", re.S)
_PY_PREFIX_KW_RE = re.compile(r"prefix\s*=\s*['\"]([^'\"]*)['\"]")

_APP_ROOT_RE = re.compile(r"\b(?:fastify|Fastify|express|Koa|Hono|FastAPI|Flask|NestFactory\.create)\s*\(|"
                          r"\bnew\s+(?:Hono|Koa|Elysia)\b|\bcreateServer\s*\(|"
                          r"\bhttp\.NewServeMux\(|\bListenAndServe\(|\bgin\.(?:Default|New)\(|\bchi\.NewRouter\(|"
                          r"\becho\.New\(")

_PARAM_SEG_RE = re.compile(r"^(?::\w+\??|\{[^}]*\}|<[^>]*>|\[[^\]]*\]|\*\w*|\$\w+|_+)$")


@dataclass
class RouteDecl:
    method: str
    path: str
    src: SourceFile
    line: int
    #: The router variable it is declared on, where the syntax names it (Go).
    recv: str = ""


@dataclass
class Mount:
    prefix: str
    ident: str
    src: SourceFile
    line: int


@dataclass
class RouteMatch:
    method: str
    pattern: str
    decl: RouteDecl
    mounts: list[Mount] = field(default_factory=list)

    def source(self) -> str:
        return f"{self.decl.src.path}:{self.decl.line}"

    def mount_source(self) -> str:
        return f"{self.mounts[-1].src.path}:{self.mounts[-1].line}" if self.mounts else ""

    def cite(self) -> str:
        chain = [f"{posixpath.basename(m.src.path)}:{m.line} prefix {m.prefix or '/'}" for m in self.mounts]
        return " + ".join(chain + [f"{posixpath.basename(self.decl.src.path)}:{self.decl.line}"])

    def as_route_source(self) -> dict:
        out = {"method": self.method, "path": self.pattern, "source": self.source(),
               "resolved_from_code": True}
        if self.mounts:
            out["mount_source"] = self.mount_source()
        return out


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _join(*parts: str) -> str:
    segs = [s for p in parts for s in (p or "").split("/") if s]
    return "/" + "/".join(segs)


def _methods(raw: str) -> list[str]:
    return [m.upper() for m in re.findall(r"\w+", raw) if m.upper() in _METHODS + ("ALL",)]


def _file_prefix(src: SourceFile) -> str:
    m = _NEST_CTRL_RE.search(src.text)
    if m:
        return m.group(1)
    m = _PY_ROUTER_PREFIX_RE.search(src.text)
    return m.group(2) if m else ""


def _subst(path: str, src: SourceFile) -> str | None:
    """A template path with each ``${this.path}``-style part replaced by the
    string the same file assigns to that name (``public path = '/envelopes'``);
    None when one is not a string the file sets."""
    def value(m: re.Match) -> str:
        name = re.escape(m.group(1).split(".")[-1])
        v = re.search(rf"\b{name}\s*(?::\s*[\w<>\[\]| ]+)?\s*=\s*['\"`]([^'\"`$]*)['\"`]", src.text)
        if v is None:
            raise LookupError
        return v.group(1)
    try:
        out = re.sub(r"\$\{([\w.]+)\}", value, path)
    except LookupError:
        return None
    return out if out.startswith("/") or not out else "/" + out


_DECL_CACHE: dict[tuple, list] = {}


def route_decls(src: SourceFile) -> list[RouteDecl]:
    key = (src.repo, src.path, src.ref, len(src.text), hash(src.text))
    if key not in _DECL_CACHE:
        if len(_DECL_CACHE) > 2000:
            _DECL_CACHE.clear()
        _DECL_CACHE[key] = _route_decls(src)
    return [RouteDecl(d.method, d.path, src, d.line, d.recv) for d in _DECL_CACHE[key]]


def _route_decls(src: SourceFile) -> list[RouteDecl]:
    text = src.text
    out: list[RouteDecl] = []
    for m in _CALL_DECL_RE.finditer(text):
        path = _subst(m.group(2), src)
        if path is not None:
            out.append(RouteDecl(m.group(1).upper(), path, src, _line(text, m.start())))
    for m in _PY_DECL_RE.finditer(text):
        verb = m.group(1).upper()
        methods = _methods(_PY_PREFIX_KW_RE.sub("", m.group(3) or "")) if verb in ("ROUTE", "API_ROUTE") else [verb]
        for meth in methods or ["GET"]:
            out.append(RouteDecl(meth, m.group(2), src, _line(text, m.start())))
    for m in _GO_DECL_RE.finditer(text):
        out.append(RouteDecl(m.group(2), m.group(3), src, _line(text, m.start()), m.group(1)))
    for m in _NEST_DECL_RE.finditer(text):
        out.append(RouteDecl(m.group(1).upper(), m.group(2) or "", src, _line(text, m.start())))
    # Route option objects (fastify ``route({method, url})``, hapi, …): pair
    # each method with the nearest url/path key.
    urls = [(u.start(), u.group(1)) for u in _OBJ_URL_RE.finditer(text)]
    for m in _OBJ_METHOD_RE.finditer(text):
        near = [(abs(pos - m.start()), u) for pos, u in urls if abs(pos - m.start()) < 400]
        if not near:
            continue
        url = min(near)[1]
        for meth in _methods(m.group(1)):
            out.append(RouteDecl(meth, url, src, _line(text, m.start())))
    return out


def mounts(src: SourceFile) -> list[Mount]:
    text = src.text
    out = [Mount(m.group(2), m.group(1), src, _line(text, m.start())) for m in _FASTIFY_MOUNT_RE.finditer(text)]
    out += [Mount(m.group(1), m.group(2), src, _line(text, m.start())) for m in _USE_MOUNT_RE.finditer(text)]
    for m in _GO_MOUNT_RE.finditer(text):
        idents = re.findall(r"\b([A-Za-z_]\w*)\b", m.group(2))
        if idents:
            out.append(Mount(m.group(1), idents[-1], src, _line(text, m.start())))
    for m in _PY_MOUNT_RE.finditer(text):
        p = _PY_PREFIX_KW_RE.search(m.group(2) or "")
        out.append(Mount(p.group(1) if p else "", m.group(1), src, _line(text, m.start())))
    return out


_ROUTER_SUFFIX_RE = re.compile(r"(?:routes?|router|plugin|controller|handlers?|api|module)$", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _stem(path: str) -> str:
    base = posixpath.basename(path)
    stem = base.rsplit(".", 1)[0]
    if stem in ("index", "__init__", "mod"):
        stem = posixpath.basename(posixpath.dirname(path))
    return stem


def import_spec(src: SourceFile, ident: str) -> str | None:
    """The module path ``ident`` is imported from in ``src``, if it is."""
    head = re.split(r"[.(]", ident)[0]
    if not head:
        return None
    h = re.escape(head)
    pats = [
        rf"import\s+{h}\s*(?:,\s*\{{[^}}]*\}})?\s+from\s+['\"]([^'\"]+)['\"]",
        rf"import\s+\*\s+as\s+{h}\s+from\s+['\"]([^'\"]+)['\"]",
        rf"import\s+(?:\w+\s*,\s*)?\{{[^}}]*\b(?:\w+\s+as\s+)?{h}\b[^}}]*\}}\s+from\s+['\"]([^'\"]+)['\"]",
        rf"\b(?:const|let|var)\s+(?:{h}|\{{[^}}]*\b{h}\b[^}}]*\}})\s*=\s*require\(\s*['\"]([^'\"]+)['\"]",
        rf"^\s*from\s+([\w.]+)\s+import\s+[^\n]*\b{h}\b",
        rf"^\s*import\s+([\w.]+)\s+as\s+{h}\b",
    ]
    for p in pats:
        m = re.search(p, src.text, re.M)
        if m:
            return m.group(1)
    return None


_EXTS = (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx", ".py", ".go")


def resolve_spec(src_path: str, spec: str, known: set[str]) -> str | None:
    """A path in ``known`` that an import spec names, relative or dotted-Python."""
    if not spec:
        return None
    cands: list[str] = []
    if spec.startswith("."):
        if "/" in spec or spec in (".", ".."):
            base = posixpath.normpath(posixpath.join(posixpath.dirname(src_path), spec))
        else:  # Python relative: .routes.chat
            dots = len(spec) - len(spec.lstrip("."))
            d = posixpath.dirname(src_path)
            for _ in range(dots - 1):
                d = posixpath.dirname(d)
            base = posixpath.join(d, spec.lstrip(".").replace(".", "/"))
        base = re.sub(r"\.(?:js|mjs|cjs)$", "", base)
        cands = [base + e for e in _EXTS] + [posixpath.join(base, "index" + e) for e in _EXTS] + [
            posixpath.join(base, "__init__.py"), base]
        for c in cands:
            if c in known:
                return c
        return None
    # Aliased or package imports: match on the trailing path.
    tail = re.sub(r"^[@~#]/?", "", spec).replace(".", "/") if "/" not in spec else re.sub(r"^[@~#][\w-]*/", "", spec)
    tail = re.sub(r"/(?:js|mjs|cjs)$", "", tail)
    ends = [tail + e for e in _EXTS] + [posixpath.join(tail, "index" + e) for e in _EXTS] + [
        posixpath.join(tail, "__init__.py")]
    hits = [k for k in known if any(k == e or k.endswith("/" + e) for e in ends)]
    return hits[0] if len(hits) == 1 else None


def _defines(src: SourceFile, ident: str) -> bool:
    h = re.escape(re.split(r"[.(]", ident)[0])
    return bool(re.search(rf"\b(?:function|const|let|var|def|class)\s+{h}\b", src.text))


def _mount_targets(index: SourceIndex, m: Mount) -> list[SourceFile]:
    same_repo = {p: f for (r, p), f in index.files.items() if r == m.src.repo}
    spec = import_spec(m.src, m.ident)
    if spec:
        p = resolve_spec(m.src.path, spec, set(same_repo))
        return [same_repo[p]] if p else []
    if _defines(m.src, m.ident):
        return [m.src]
    # No import seen (Python ``routes.chat.router``, a re-export): a router
    # file whose name is the identifier without its suffix.
    leaf = re.split(r"[.(]", m.ident)
    names = {_norm(_ROUTER_SUFFIX_RE.sub("", x)) for x in leaf if x} - {""}
    named = [f for p, f in same_repo.items() if f is not m.src and _norm(_stem(p)) in names]
    if named:
        return named
    # Go: the mounted handler is a mux variable; its routes are the ones
    # declared on a variable of that name (often a parameter in another file).
    on_recv = [f for f in same_repo.values() if any(d.recv == m.ident for d in route_decls(f))]
    if on_recv:
        return on_recv
    # A loop variable (``routes.forEach(route => app.use('/', route.router))``):
    # every router-building file the app could have been handed.
    return [f for p, f in same_repo.items() if f is not m.src and not _TEST_PATH_RE.search(p)
            and _MAKES_ROUTER_RE.search(f.text)]


def _prefix_chains(index: SourceIndex, target: SourceFile, all_mounts: list[Mount], depth: int = 0
                   ) -> list[tuple[str, list[Mount]]] | None:
    """Every ``(prefix, mounts outermost first)`` that reaches ``target``; None
    when nothing read mounts it and it does not create the app."""
    if depth > 4:
        return None
    via = [m for m in all_mounts if m.src is not target and target in _mount_targets(index, m)]
    if not via:
        return [("", [])] if _APP_ROOT_RE.search(target.text) else None
    out: list[tuple[str, list[Mount]]] = []
    for m in via:
        outer = _prefix_chains(index, m.src, all_mounts, depth + 1)
        for pre, chain in outer or []:
            out.append((_join(pre, m.prefix), chain + [m]))
    return out or None


def _segments(path: str) -> list[str]:
    return [s for s in (path or "").strip().split("/") if s]


def _seg_eq(a: str, b: str) -> bool:
    return bool(_PARAM_SEG_RE.match(a) or _PARAM_SEG_RE.match(b) or a == b)


def path_matches(pattern: str, path: str) -> bool:
    a, b = _segments(pattern.lower()), _segments(path.lower())
    if a and a[-1] in ("*", "**", "(.*)"):
        return len(b) >= len(a) - 1 and all(_seg_eq(x, y) for x, y in zip(a[:-1], b))
    return len(a) == len(b) and all(_seg_eq(x, y) for x, y in zip(a, b))


def _tail_matches(own: str, path: str) -> bool:
    """The called path ends with the declared one, with at least one fixed
    segment equal on both sides (a lone ``/:id`` matches every path)."""
    a, b = _segments(own.lower()), _segments(path.lower())
    if not a or len(b) < len(a):
        return False
    tail = b[-len(a):]
    fixed = [(x, y) for x, y in zip(a, tail) if not _PARAM_SEG_RE.match(x) and not _PARAM_SEG_RE.match(y)]
    return bool(fixed) and all(x == y for x, y in fixed) and path_matches(own, "/" + "/".join(tail))


@dataclass
class RouteSearch:
    match: RouteMatch | None
    #: The route's own path was declared, but no read file mounts its file.
    unmounted: list[RouteDecl]
    searched: list[str]

    files_read: int = 0

    def where(self) -> str:
        if not self.searched:
            return f"no route declaration or registration in the {self.files_read} file(s) read"
        names = ", ".join(posixpath.basename(p) for p in self.searched[:4]) + (" …" if len(self.searched) > 4 else "")
        return f"searched {len(self.searched)} router file(s): {names}"


def resolve_route(index: SourceIndex | None, method: str, path: str,
                  alt_paths: list[str] | tuple[str, ...] = ()) -> RouteSearch:
    if index is None or isinstance(index, str):
        return RouteSearch(None, [], [])
    method = method.upper()
    # Test files build throwaway apps with their own prefixes: not the router.
    files = [f for f in index.files.values() if is_source_path(f.path) and not _TEST_PATH_RE.search(f.path)]
    decls = [d for f in files for d in route_decls(f)]
    all_mounts = [m for f in files for m in mounts(f)]
    searched = sorted({d.src.path for d in decls} | {m.src.path for m in all_mounts})
    paths = [path] + [p for p in alt_paths if p != path]
    unmounted: list[RouteDecl] = []
    for d in decls:
        if d.method not in (method, "ALL"):
            continue
        own = _join(_file_prefix(d.src), d.path)
        chains = _prefix_chains(index, d.src, all_mounts)
        if chains is None:
            # The tail matches, but the registration that prefixes it was not read.
            if any(_tail_matches(own, p) for p in paths):
                unmounted.append(d)
            continue
        for pre, chain in chains:
            full = _join(pre, own)
            if any(path_matches(full, p) for p in paths):
                return RouteSearch(RouteMatch(method, full, d, chain), [], searched, len(files))
    return RouteSearch(None, unmounted, searched, len(files))


# --- reading the router files ------------------------------------------------

_ENTRY_STEMS = {"app", "server", "index", "main", "routes", "route", "router", "routers", "plugins", "api", "urls",
                "register", "http"}
MAX_ROUTER_FILES = 16


async def read_router_context(index: SourceIndex, github, called_paths: list[str]) -> list[str]:
    """Read app entry files and router files a called path names, at each
    repo's PR ref, so a route is resolved from its registration even when no
    case cites it. Returns the paths read. Never raises; a file it could not
    read simply stays unread (the route stays unchecked, with where it looked)."""
    read: list[str] = []
    seg_names = {_norm(s) for p in called_paths for s in _segments(p) if not _PARAM_SEG_RE.match(s)} - {""}
    for repo, ref in list(index.refs.items()):
        tree = index.trees.get(repo)
        if not tree or not ref:
            continue
        code = [p for p in tree if p.endswith(_EXTS) and not _TEST_PATH_RE.search(p)]
        have = {p for (r, p) in index.files if r == repo}
        anchors = [p for p in have] + [p for p in code if _norm(_stem(p)) in seg_names
                                       or _norm(_ROUTER_SUFFIX_RE.sub("", _stem(p))) in seg_names]
        dirs: set[str] = set()
        for a in anchors:
            d = posixpath.dirname(a)
            for _ in range(4):
                dirs.add(d)
                if not d:
                    break
                d = posixpath.dirname(d)
        want = [p for p in code if p not in have and (
            (posixpath.dirname(p) in dirs and _stem(p).lower() in _ENTRY_STEMS)
            or _norm(_stem(p)) in seg_names or _norm(_ROUTER_SUFFIX_RE.sub("", _stem(p))) in seg_names)]
        for _ in range(2):  # entry files, then what they mount
            want = list(dict.fromkeys(want))[:MAX_ROUTER_FILES - len(read)]
            if not want:
                break
            try:
                got = await github.fetch_files_at_ref(repo, ref, want, max_bytes=400_000) or {}
            except Exception:  # noqa: BLE001 — unread stays unread, never "absent"
                got = {}
            for p, text in got.items():
                if isinstance(text, str) and (repo, p) not in index.files:
                    index.files[(repo, p)] = SourceFile(repo, p, ref, text)
                    read.append(p)
            known = set(code)
            nxt = []
            for p in got:
                src = index.files.get((repo, p))
                for m in mounts(src) if src else []:
                    spec = import_spec(src, m.ident)
                    t = resolve_spec(p, spec, known) if spec else None
                    if t and (repo, t) not in index.files:
                        nxt.append(t)
            want = nxt
    return read
