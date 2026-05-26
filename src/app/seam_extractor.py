"""Cross-project seam extraction for multi-ticket test plans.

When a multi-ticket request spans repositories (e.g. ``files-ui`` calling
``agent-calculator`` via HTTP), the LLM needs to know *where* the projects
talk to each other so it can write integration tests that verify the
relationship — not just per-side behaviour.

This module reads the already-fetched PR diffs and extracts two lists per
repo:

* **Exports** — surfaces a repo *exposes* (HTTP routes, exported symbols,
  event/topic names).
* **Calls** — surfaces a repo *consumes* (fetch/axios URLs, in-house package
  imports, event subscribers).

Intersecting across repos gives:

* **Verified seams** — an export in repo A with a matching call in repo B.
  The LLM is asked to write tests for these.
* **Suspected seams** — calls or exports whose counterpart was not located
  in any diff. The LLM is asked to write tests AND attach a grounding
  warning.

Regex-only, intentionally. The goal is high-precision matches on the
common patterns (REST routes, package imports, named events); subtler seams
(string-built URLs, runtime-resolved topics) fall through to the
"suspected" pathway with a grounding warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re


MAX_PATCH_LINES_PER_FILE = 500


@dataclass(frozen=True)
class Export:
    """A surface that a repo exposes (others may call it)."""

    repo: str
    kind: str  # "http_route" | "event" | "package_import"
    identifier: str  # normalized: e.g. "GET /api/orders/{}", "order.created", "@acme/billing.computeTax"
    file: str
    line: int


@dataclass(frozen=True)
class Call:
    """A surface that a repo calls (others may expose it)."""

    repo: str
    kind: str
    identifier: str
    file: str
    line: int


@dataclass
class Seam:
    """A producer/consumer pair detected across repos."""

    kind: str
    identifier: str
    producer: dict | None  # {repo, file, line}
    consumer: dict | None
    verified: bool

    def to_dict(self) -> dict:
        out: dict = {
            "kind": self.kind,
            "identifier": self.identifier,
            "verified": self.verified,
        }
        if self.producer is not None:
            out["producer"] = self.producer
        if self.consumer is not None:
            out["consumer"] = self.consumer
        return out


# ── Path normalization ──────────────────────────────────────────────────────

_PARAM_PLACEHOLDER_RE = re.compile(r"(:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")
_QUERY_STRING_RE = re.compile(r"\?.*$")


def normalize_route(path: str) -> str:
    """Normalize a route path so ``/orders/:id``, ``/orders/{id}`` and
    ``/orders/42`` all compare equal as ``/orders/{}``.

    Also strips query strings and trailing slashes.
    """
    if not path:
        return path
    p = _QUERY_STRING_RE.sub("", path).rstrip("/")
    p = _PARAM_PLACEHOLDER_RE.sub("{}", p)
    p = _NUMERIC_SEGMENT_RE.sub("/{}", p)
    return p or "/"


# ── Regex patterns ──────────────────────────────────────────────────────────

# FastAPI / Starlette: @app.get("/x"), @router.post("/y")
_PY_ROUTE_RE = re.compile(
    r"""@\s*(?:app|router|api|bp)\s*\.\s*(get|post|put|patch|delete|head|options)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# Flask: @app.route("/x", methods=["POST"]) — capture path, default to GET
_PY_FLASK_ROUTE_RE = re.compile(
    r"""@\s*(?:app|bp|blueprint)\s*\.\s*route\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# Express / Koa: app.get("/x", handler) — exclude `.use(` to skip middleware
_JS_ROUTE_RE = re.compile(
    r"""\b(?:app|router|api)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

# Python def at module top-level (no leading indent) — heuristic for exported helpers
_PY_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# JS/TS exported function/class/const
_JS_EXPORT_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# Event publish
_EVENT_PUBLISH_RE = re.compile(
    r"""\b(?:publish|emit|send|dispatch|broadcast)\s*\(\s*["']([A-Za-z][\w.\-:]+)["']""",
    re.IGNORECASE,
)
# Event subscribe
_EVENT_SUBSCRIBE_RE = re.compile(
    r"""\b(?:subscribe|on|consume|listen|handle)\s*\(\s*["']([A-Za-z][\w.\-:]+)["']""",
    re.IGNORECASE,
)

# Outbound HTTP calls
_FETCH_RE = re.compile(r"""\bfetch\s*\(\s*["']([^"']+)["']""")
_AXIOS_RE = re.compile(
    r"""\baxios\s*\.\s*(get|post|put|patch|delete)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_HTTPX_RE = re.compile(
    r"""\b(?:client|httpx|requests)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

# In-house package imports — scope allowlist is configurable
_JS_IMPORT_RE = re.compile(
    r"""(?:from|require)\s*\(?\s*["'](@[^"'/]+/[^"']+)["']"""
)
_PY_IMPORT_RE = re.compile(r"^(?:from|import)\s+([A-Za-z_][\w.]*)")


# ── Patch helpers ───────────────────────────────────────────────────────────

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _iter_added_lines(patch: str):
    """Yield ``(line_number, line_text)`` for every added line in a unified
    diff patch. Line numbers are in the *new* file."""
    if not patch:
        return
    current_line = 0
    seen = 0
    for raw in patch.split("\n"):
        seen += 1
        if seen > MAX_PATCH_LINES_PER_FILE:
            return
        m = _HUNK_HEADER_RE.match(raw)
        if m:
            current_line = int(m.group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            yield current_line, raw[1:]
            current_line += 1
        elif raw.startswith("-"):
            # deletion: don't advance new-file line counter
            continue
        else:
            current_line += 1


def _looks_like_python(filename: str) -> bool:
    return filename.endswith(".py")


def _looks_like_js(filename: str) -> bool:
    return filename.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))


# ── Extraction ──────────────────────────────────────────────────────────────


def _allowed_scopes(scopes: list[str] | None) -> list[str]:
    if not scopes:
        return []
    return [s.strip().rstrip("/") for s in scopes if s and s.strip()]


def extract_exports(
    repo: str,
    filename: str,
    patch: str,
    *,
    package_scopes: list[str] | None = None,
) -> list[Export]:
    """Pull surfaces this repo exposes from a single file's diff patch."""
    if not patch or not filename:
        return []

    out: list[Export] = []
    is_py = _looks_like_python(filename)
    is_js = _looks_like_js(filename)
    scopes = _allowed_scopes(package_scopes)

    for line_no, text in _iter_added_lines(patch):
        # HTTP routes
        if is_py:
            m = _PY_ROUTE_RE.search(text)
            if m:
                method = m.group(1).upper()
                path = normalize_route(m.group(2))
                out.append(Export(repo, "http_route", f"{method} {path}", filename, line_no))
                continue
            m = _PY_FLASK_ROUTE_RE.search(text)
            if m:
                path = normalize_route(m.group(1))
                out.append(Export(repo, "http_route", f"GET {path}", filename, line_no))
                continue
        if is_js:
            m = _JS_ROUTE_RE.search(text)
            if m:
                method = m.group(1).upper()
                path = normalize_route(m.group(2))
                out.append(Export(repo, "http_route", f"{method} {path}", filename, line_no))
                continue

        # Events published
        m = _EVENT_PUBLISH_RE.search(text)
        if m:
            out.append(Export(repo, "event", m.group(1), filename, line_no))
            continue

        # Exported symbols from in-house packages (heuristic: filename in a
        # likely-published location). Without this gate, every exported
        # symbol would be flagged as a cross-project surface.
        looks_published = (
            "/packages/" in filename
            or filename.startswith("packages/")
            or "/lib/" in filename
            or filename.startswith("lib/")
        )
        if looks_published:
            if is_js:
                m = _JS_EXPORT_RE.match(text)
                if m:
                    out.append(
                        Export(
                            repo,
                            "package_import",
                            f"{repo}.{m.group(1)}",
                            filename,
                            line_no,
                        )
                    )
                    continue
            if is_py:
                m = _PY_DEF_RE.match(text)
                if m and not m.group(1).startswith("_"):
                    out.append(
                        Export(
                            repo,
                            "package_import",
                            f"{repo}.{m.group(1)}",
                            filename,
                            line_no,
                        )
                    )
                    continue

        # Package re-exports referencing an in-house scope. Treats
        # ``export * from "@org/pkg"`` as the consuming repo's call rather
        # than an export, so skip here.
        _ = scopes  # silence unused warning (scopes used by extract_calls)

    return out


def extract_calls(
    repo: str,
    filename: str,
    patch: str,
    *,
    package_scopes: list[str] | None = None,
) -> list[Call]:
    """Pull surfaces this repo *calls* from a single file's diff patch."""
    if not patch or not filename:
        return []

    out: list[Call] = []
    is_py = _looks_like_python(filename)
    is_js = _looks_like_js(filename)
    scopes = _allowed_scopes(package_scopes)

    for line_no, text in _iter_added_lines(patch):
        # fetch(URL)
        m = _FETCH_RE.search(text)
        if m:
            url = m.group(1)
            if _looks_like_route(url):
                ident = f"GET {normalize_route(url)}"
                out.append(Call(repo, "http_route", ident, filename, line_no))

        # axios.<method>(URL)
        m = _AXIOS_RE.search(text)
        if m:
            method = m.group(1).upper()
            url = m.group(2)
            if _looks_like_route(url):
                ident = f"{method} {normalize_route(url)}"
                out.append(Call(repo, "http_route", ident, filename, line_no))

        # httpx / requests / generic client
        if is_py:
            m = _HTTPX_RE.search(text)
            if m:
                method = m.group(1).upper()
                url = m.group(2)
                if _looks_like_route(url):
                    ident = f"{method} {normalize_route(url)}"
                    out.append(Call(repo, "http_route", ident, filename, line_no))

        # Event subscribers
        m = _EVENT_SUBSCRIBE_RE.search(text)
        if m:
            out.append(Call(repo, "event", m.group(1), filename, line_no))

        # In-house package imports — only when the import scope is in the
        # configured allowlist
        if scopes:
            if is_js:
                m = _JS_IMPORT_RE.search(text)
                if m:
                    pkg = m.group(1)
                    if any(pkg == s or pkg.startswith(s + "/") for s in scopes):
                        out.append(
                            Call(repo, "package_import", pkg, filename, line_no)
                        )
            if is_py:
                m = _PY_IMPORT_RE.match(text)
                if m:
                    mod = m.group(1)
                    if any(mod == s or mod.startswith(s + ".") for s in scopes):
                        out.append(
                            Call(repo, "package_import", mod, filename, line_no)
                        )

    return out


def _looks_like_route(url: str) -> bool:
    """Heuristic: is this URL string a meaningful HTTP route worth matching?

    Filters out things like ``"data:image/png;base64,..."``,
    ``"chrome://..."``, ``"mailto:..."``, and bare absolute URLs to
    external domains.
    """
    if not url:
        return False
    if url.startswith("data:") or url.startswith("mailto:") or url.startswith("tel:"):
        return False
    if url.startswith("//"):
        return False
    # Allow relative paths and same-origin absolute paths
    if url.startswith("/"):
        return True
    # http(s)://host/path — strip host and check path
    m = re.match(r"^https?://[^/]+(/.*)?$", url, re.IGNORECASE)
    if m:
        path = m.group(1) or "/"
        return bool(path) and path != "/"
    return False


# ── Catalog construction ────────────────────────────────────────────────────


@dataclass
class SeamCatalog:
    """Result of running the extractor across one multi-ticket request."""

    verified: list[Seam] = field(default_factory=list)
    suspected: list[Seam] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repos": self.repos,
            "verified_seams": [s.to_dict() for s in self.verified],
            "suspected_seams": [s.to_dict() for s in self.suspected],
        }

    @property
    def is_empty(self) -> bool:
        return not self.verified and not self.suspected


def build_seam_catalog(
    tickets_data: list[dict],
    *,
    package_scopes: list[str] | None = None,
) -> SeamCatalog:
    """Walk every PR diff across every ticket and build a seam catalog.

    ``tickets_data`` is the list of ticket dicts passed to the LLM client
    (each has ``development_info.pull_requests[*].files_changed[*].patch``).

    A seam is *verified* when an export and a call refer to the same
    identifier in *different* repos; otherwise the unmatched side is
    *suspected* so the LLM still considers it but flags the test.
    """
    exports: list[Export] = []
    calls: list[Call] = []
    repos_seen: set[str] = set()

    for ticket in tickets_data or []:
        dev_info = ticket.get("development_info") or {}
        for pr in dev_info.get("pull_requests") or []:
            repo = (pr.get("repository") or "").strip()
            if not repo:
                continue
            repos_seen.add(repo)
            for fc in pr.get("files_changed") or []:
                if (fc.get("status") or "") == "removed":
                    continue
                filename = fc.get("filename") or ""
                patch = fc.get("patch") or ""
                if not filename or not patch:
                    continue
                exports.extend(
                    extract_exports(repo, filename, patch, package_scopes=package_scopes)
                )
                calls.extend(
                    extract_calls(repo, filename, patch, package_scopes=package_scopes)
                )

    return _intersect(exports, calls, repos_seen)


def _intersect(
    exports: list[Export], calls: list[Call], repos_seen: set[str]
) -> SeamCatalog:
    """Pair each call with an export in a different repo. Unmatched
    entries become suspected seams.
    """
    # Index exports by (kind, identifier) → first occurrence per repo
    export_index: dict[tuple[str, str], list[Export]] = {}
    for e in exports:
        export_index.setdefault((e.kind, e.identifier), []).append(e)

    verified: list[Seam] = []
    suspected: list[Seam] = []
    seen_pairs: set[tuple[str, str, str, str]] = set()
    matched_export_ids: set[tuple[str, str, str]] = set()

    for c in calls:
        producers = [
            e for e in export_index.get((c.kind, c.identifier), []) if e.repo != c.repo
        ]
        if producers:
            e = producers[0]
            key = (c.kind, c.identifier, e.repo, c.repo)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            matched_export_ids.add((e.kind, e.identifier, e.repo))
            verified.append(
                Seam(
                    kind=c.kind,
                    identifier=c.identifier,
                    producer={"repo": e.repo, "file": e.file, "line": e.line},
                    consumer={"repo": c.repo, "file": c.file, "line": c.line},
                    verified=True,
                )
            )
        else:
            # Suspected: call without a matching export in another repo
            suspected.append(
                Seam(
                    kind=c.kind,
                    identifier=c.identifier,
                    producer=None,
                    consumer={"repo": c.repo, "file": c.file, "line": c.line},
                    verified=False,
                )
            )

    # Exports without any matching call across repos → suspected too
    for e in exports:
        if (e.kind, e.identifier, e.repo) in matched_export_ids:
            continue
        suspected.append(
            Seam(
                kind=e.kind,
                identifier=e.identifier,
                producer={"repo": e.repo, "file": e.file, "line": e.line},
                consumer=None,
                verified=False,
            )
        )

    # Drop suspected duplicates by (kind, identifier, side, repo)
    deduped: list[Seam] = []
    seen_suspected: set[tuple] = set()
    for s in suspected:
        side = "producer" if s.producer else "consumer"
        repo = (s.producer or s.consumer or {}).get("repo")
        key = (s.kind, s.identifier, side, repo)
        if key in seen_suspected:
            continue
        seen_suspected.add(key)
        deduped.append(s)

    return SeamCatalog(
        verified=verified,
        suspected=deduped,
        repos=sorted(repos_seen),
    )


def classify_multi_ticket_mode(tickets_data: list[dict]) -> str:
    """Return ``"cross_project"`` when the request spans >1 repo, else
    ``"single_repo"``. A ticket without development info is ignored for
    the repo count.
    """
    repos: set[str] = set()
    for ticket in tickets_data or []:
        dev_info = ticket.get("development_info") or {}
        for pr in dev_info.get("pull_requests") or []:
            repo = (pr.get("repository") or "").strip()
            if repo:
                repos.add(repo)
    return "cross_project" if len(repos) > 1 else "single_repo"
