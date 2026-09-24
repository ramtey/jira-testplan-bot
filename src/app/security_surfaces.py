"""Security negative-test surface detection for the test-plan generator.

Why this module exists
----------------------
Plans are written from acceptance criteria, and acceptance criteria are
written from the intended user's point of view. Nobody writes "…and an
anonymous caller who guesses the draft UUID is denied" into an AC, so no
plan derived from that AC ever tested it. SK-2702 through SK-2707 are the
bill for that: six security bugs on Agent Calculator, at least five of them
reachable from the diff alone.

The worked example this module exists to prevent is SK-2702. The public
``shareFlowRecipient.authenticate`` procedure took a caller-controlled
``preview`` boolean that reached ``ShareFlowService.lookupLink`` and made a
draft behave as active, without checking that the caller was the sender.
Anyone holding a draft UUID could mint a guest session and read an unsent
estimate, PDF or audio walkthrough. The AC for the ticket that shipped that
procedure said the sender can preview a draft — which is true, and which a
happy-path plan verified, and which says nothing at all about who else can.

So this rule does NOT read the AC. It reads the diff, and it fires on the
file paths and patch content that mark a risky surface:

* routers and routes (tRPC procedures, REST/HTTP handlers)
* auth, middleware, guards, permission policies
* upload and media handling
* share-flow and capability-link code
* WebSocket / SSE / streaming routes
* vendor-backed and paid-AI calls

Two responsibilities, the same split as :mod:`shared_component_fanout`:

1. Detect which risky surfaces a ticket's diff touches, and which security
   categories follow from them. See :func:`detect_security_surfaces`.

2. Render a prompt block that asks for one case per persona/protocol cell,
   grounded, API-level, and never collapsed. See
   :func:`render_security_guidance`.

Nothing here decides whether a case is right. It hands the model the
surfaces, the personas and the shape, and the model writes the cases —
same contract as every other guidance block.

Deliberately narrow
-------------------
A category is only emitted when the diff gives evidence for it. A CSS
ticket touches none of these paths and gets no security section at all; a
ticket that only changes an upload handler gets upload and revoked-account
cases and no rate-limit boilerplate. ``client_flag`` goes further and
requires an actual access-affecting parameter name to have appeared in an
added patch line — there is no generic "test your flags" case, because a
case that can't name the flag can't be run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ── Surface classification ───────────────────────────────────────────────────

# Path patterns that mark each risky surface. Matched case-insensitively
# against the full path of every file in every linked PR. Seeded from the
# agent-calculator layout named in SK-2702/SK-2705; extend when a new shape
# appears rather than loosening an existing pattern.
_SURFACE_PATH_PATTERNS: dict[str, tuple[str, ...]] = {
    "trpc_router": (
        r"/api/routers?/",
        r"/trpc\b",
        r"/trpc\.(?:ts|js)$",
        r"/routers?/[^/]+\.(?:ts|js)$",
    ),
    "raw_route": (
        r"/routes?/",
        r"/route\.(?:ts|tsx|js|jsx)$",
        r"/controllers?/",
        r"/handlers?/",
        r"/server/src/index\.(?:ts|js)$",
        r"/pages/api/",
        # Document-generation entry points. Named explicitly because SK-2705
        # lists PDF alongside upload and voice as a protected protocol that
        # re-implements its own auth check, and a handler called
        # `getCalculationPdf.ts` matches none of the route-shaped patterns
        # above. Anchored to a server-side path segment so a frontend
        # `exportUtils.ts` is not mistaken for an endpoint.
        r"/(?:server|api|backend)/.*?(?:pdf|download|export)[^/]*\.(?:ts|js|py)$",
    ),
    "stream_route": (
        r"/[^/]*(?:websocket|socket|sse|stream|live)[^/]*\.(?:ts|js|py)$",
        r"/[^/]*\bws\b[^/]*\.(?:ts|js|py)$",
    ),
    "auth_middleware": (
        r"/middlewares?\b",
        r"/[^/]*auth[^/]*\.(?:ts|js|py)$",
        r"/guards?/",
        r"/policies/",
        r"/[^/]*permissions?[^/]*\.(?:ts|js|py)$",
        r"/session\.(?:ts|js|py)$",
    ),
    "upload_media": (
        r"/uploads?\b",
        r"/media\b",
        r"/attachments?\b",
        r"/avatars?\b",
        r"/[^/]*profile-?(?:media|image|photo)[^/]*",
        r"/storage/",
    ),
    "share_capability": (
        r"/share[-_]?flow",
        r"/share\b",
        r"/invites?\b",
        r"/public-?links?",
        r"/capabilit",
    ),
    "vendor_ai": (
        r"/(?:ai|llm|openai|anthropic|tts|stt|voice|speech)[-_/]",
        r"/[^/]*(?:-tts|tts-|llm-|-llm)[^/]*\.(?:ts|js|py)$",
        r"/vendors?/",
        r"/integrations?/",
    ),
}

# Patch-content patterns. A file whose PATH says nothing can still be a
# route or an upload handler; these read the added lines to catch it. They
# are a second chance at classification, never a veto on the path match.
_SURFACE_PATCH_PATTERNS: dict[str, tuple[str, ...]] = {
    "trpc_router": (
        r"\b(?:public|protected|private)Procedure\b",
        r"\.(?:query|mutation)\s*\(\s*(?:async\s*)?\(",
        r"\brouter\s*\(\s*\{",
    ),
    "raw_route": (
        r"\b(?:app|router|server|fastify|hono|api)\.(?:get|post|put|patch|delete|all)\s*\(",
        r"@(?:app|router|bp)\.(?:route|get|post|put|delete)\s*\(",
        r"\bexport\s+(?:async\s+)?function\s+(?:GET|POST|PUT|PATCH|DELETE)\b",
    ),
    "stream_route": (
        r"\bWebSocketServer\b",
        r"\bwss?\.on\s*\(",
        r"text/event-stream",
        r"\bupgrade\b\s*[:(]",
        r"socket\.io",
        r"ReadableStream",
    ),
    "auth_middleware": (
        # `protectedProcedure` / `publicProcedure` deliberately do NOT appear
        # here. They mark a tRPC ROUTER (where they are already listed) — every
        # router in the codebase uses one — and classifying a router as auth
        # middleware would pull in `unauth_sweep` and `revoked_account` on every
        # tRPC change, which is exactly the boilerplate the category map is
        # shaped to avoid. The real middleware signals are below: they are about
        # how a token is checked, not about a procedure being declared.
        r"\b(?:verify|decode|validate)(?:Id)?Token\b",
        r"\brequireAuth\b",
        r"\bjwt\.verify\b",
        r"\bgetSession\b",
        r"\bdeletedAt\b",
        r"\bisActive\b",
    ),
    "upload_media": (
        r"multipart/form-data",
        r"\bmulter\b",
        r"createPresignedPost",
        r"\bputObject\b",
        r"Buffer\.from\([^)]*base64",
        r"\bcontent-?type\b",
    ),
    "share_capability": (
        r"\bshareFlow\b",
        r"\blookupLink\b",
        r"\bcapability\b",
        r"\bguestSession\b",
    ),
    "vendor_ai": (
        r"\b(?:openai|anthropic|elevenlabs|deepgram)\b",
        r"completions\.create",
        r"\bsynthesi[sz]e\b",
        r"\bgenerateSpeech\b",
    ),
}

# Human-readable surface names, for the prompt.
SURFACE_LABELS: dict[str, str] = {
    "trpc_router": "tRPC router / procedure",
    "raw_route": "raw HTTP / REST route",
    "stream_route": "WebSocket / SSE / streaming route",
    "auth_middleware": "auth / middleware / permission policy",
    "upload_media": "upload / media handling",
    "share_capability": "share flow / capability link",
    "vendor_ai": "vendor-backed or paid-AI call",
}


# ── Access-affecting request parameters ──────────────────────────────────────

# Parameter names that, when a caller controls them, can change WHAT THE
# CALLER IS ALLOWED TO SEE OR DO. `preview` heads the list because it is
# the literal SK-2702 flag. Matched against added patch lines in files that
# classified as a router, route or share surface.
#
# This is an allowlist on purpose. A generic "audit every new parameter"
# case cannot be run and cannot be graded; a case that names `preview` can.
ACCESS_AFFECTING_PARAMS: tuple[str, ...] = (
    "preview",
    "includeRaw",
    "includeDeleted",
    "includePrivate",
    "includeInternal",
    "isAdmin",
    "admin",
    "role",
    "roles",
    "asUser",
    "onBehalfOf",
    "impersonate",
    "userId",
    "ownerId",
    "accountId",
    "orgId",
    "tenantId",
    "internal",
    "debug",
    "bypass",
    "skipAuth",
    "skipValidation",
    "force",
    "unsafe",
    "all",
    "scope",
    "visibility",
)

# A parameter name only counts when it appears in a declaration-like
# position — an input schema field, a destructured argument, a query-string
# read — rather than anywhere in the line. Keeps `role` from matching a
# comment about someone's role on the team.
_PARAM_CONTEXT_TEMPLATES: tuple[str, ...] = (
    # zod / yup / joi input schema: `preview: z.boolean()`
    r"\b{name}\s*:\s*(?:z|yup|joi|t|v)\.",
    # plain object type or destructure: `{ preview }` / `preview?: boolean`
    r"\b{name}\s*\??\s*:\s*(?:boolean|string|number|z\b)",
    r"[{,]\s*{name}\s*[},=]",
    # query-string / params read: `query.preview`, `params.get('preview')`
    r"\b(?:query|params|searchParams|body|input|req\.query)\??\.\s*{name}\b",
    r"\bget\s*\(\s*[\"']{name}[\"']\s*\)",
)


# ── Security categories ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecurityCategory:
    """One category of security negative test, with the cells it fans into.

    ``cells`` is the list of distinct cases the category REQUIRES — one per
    persona, payload or protocol. They are listed here rather than left to
    the model because "anon, other user, owner" is exactly the shape the
    plan-level dedup rules collapse into a single parameterized case, and a
    collapsed authz case passes while two of its three cells are broken.
    """

    key: str
    title: str
    why: str
    cells: tuple[str, ...]
    directive: str


SECURITY_CATEGORIES: dict[str, SecurityCategory] = {
    "authz_matrix": SecurityCategory(
        key="authz_matrix",
        title="Authorization matrix — every protected action, as every principal",
        why=(
            "A protected action is normally tested as the person allowed to do "
            "it. That proves the allow path and nothing about the deny paths, "
            "which is where the bug lives."
        ),
        cells=(
            "anonymous (no token at all)",
            "another authenticated user who does not own the record",
            "the owner (the control case — this one must still succeed)",
        ),
        directive=(
            "For EVERY protected action the diff adds or changes, emit one case "
            "per principal above. Assert the exact status code and body the "
            "implementation returns for each — read them out of the handler or "
            "middleware, and do not assume 401 vs 403 from convention. If the "
            "action is reached by a capability link (a share UUID, a draft id, "
            "a signed token in a URL), ALSO emit a case that calls it with an "
            "ID belonging to a different record — the 'I obtained a UUID' "
            "case — and one with a well-formed but never-issued ID."
        ),
    ),
    "client_flag": SecurityCategory(
        key="client_flag",
        title="Client-controlled flags — parameters that change what the caller may see",
        why=(
            "A request parameter that widens access is legitimate for one "
            "caller and a privilege escalation for another. SK-2702 is exactly "
            "this: `preview=true` is correct for the sender and lets anyone "
            "with a draft UUID read an unsent estimate."
        ),
        cells=(
            "the flag set by an anonymous caller",
            "the flag set by an authenticated caller who does not own the record",
            "the flag set by the legitimate owner (the control case)",
        ),
        directive=(
            "For EACH access-affecting parameter listed in the detected signals "
            "below, emit one case per principal above, sending the parameter at "
            "its access-widening value. The assertion is NOT 'the request "
            "fails' — it is that the flag does not change what this caller can "
            "read. Name the exact parameter and value in the request. Do not "
            "invent parameters that are not in the detected list."
        ),
    ),
    "unauth_sweep": SecurityCategory(
        key="unauth_sweep",
        title="Unauthenticated sweep — every changed route, called with no token",
        why=(
            "Auth on one protocol is not auth on another. A framework's "
            "protected-procedure wrapper covers its own procedures and nothing "
            "else; raw HTTP, REST and WebSocket routes each re-implement the "
            "check, and each can forget it."
        ),
        cells=(
            "one case per changed route, called with no Authorization header",
            "one case per changed route, called with a malformed/garbage token",
        ),
        directive=(
            "Emit one case PER ROUTE listed in the detected signals — not one "
            "case that sweeps them. Each names its own method, path and "
            "protocol. A route that is intentionally public gets a case too: "
            "assert the specific public response, so that a later change making "
            "it accidentally protected (or accidentally leakier) is caught."
        ),
    ),
    "revoked_account": SecurityCategory(
        key="revoked_account",
        title="Deleted / disabled account — a still-valid token, every protocol",
        why=(
            "Soft-deleting a user invalidates the account, not the token they "
            "are already holding. SK-2705: the tRPC layer denied a soft-deleted "
            "user while independently-implemented HTTP, REST and WebSocket "
            "routes checked only that the token parsed, so a deleted user kept "
            "uploads, PDF generation and paid voice operations."
        ),
        cells=(
            "one case per changed protected entry point, replayed by a "
            "soft-deleted user whose token is still cryptographically valid",
        ),
        directive=(
            "Emit one case PER ENTRY POINT listed in the detected signals, each "
            "naming its own protocol. This is a matrix, and collapsing it is "
            "the bug: the whole finding in SK-2705 was that one protocol "
            "enforced and the others did not, which a single 'deleted user is "
            "denied' case cannot see. State in `preconditions` how the deleted "
            "identity is produced and that its token must be minted BEFORE the "
            "deletion. Where the code has a database lookup for account state, "
            "add a case asserting it fails closed when that lookup errors."
        ),
    ),
    "upload_abuse": SecurityCategory(
        key="upload_abuse",
        title="Upload abuse — what is accepted, and how it is served back",
        why=(
            "An upload validator that trusts the declared MIME type accepts a "
            "script with an image label, and a store that serves it back with "
            "that same declared type turns it into stored XSS. Both halves have "
            "to be tested, and the second half is the one plans always miss."
        ),
        cells=(
            "declared MIME type does not match the file's magic bytes",
            "an SVG (or HTML) carrying a script payload",
            "malformed / truncated base64 in the request body",
            "a file whose DECODED size exceeds the limit (the encoded size "
            "under-reports it)",
            "the response headers on the SERVED file — Content-Type, "
            "X-Content-Type-Options, Cache-Control",
        ),
        directive=(
            "Emit one case per row above. The served-headers case asserts on "
            "the GET of the stored object, not on the upload response — assert "
            "the exact header values the serving code sets, and say explicitly "
            "whether a private object is cacheable. Each malformed-payload case "
            "needs `data_shape` with `provisioning: \"stub\"`: no real user "
            "account produces a signature mismatch, so this is an Engineering "
            "ask, not a fixture hunt."
        ),
    ),
    "abuse_cost": SecurityCategory(
        key="abuse_cost",
        title="Abuse and cost — repeated and concurrent calls to a paid endpoint",
        why=(
            "An endpoint backed by a paid vendor is a spend surface as well as "
            "a data surface. A limit nobody ever tested is a limit nobody knows "
            "is wired up."
        ),
        cells=(
            "N sequential calls from one principal, past the documented limit",
            "N concurrent calls from one principal (a limiter that reads and "
            "then writes a counter admits all of them)",
            "the same, as an anonymous or guest caller where the endpoint "
            "allows one",
        ),
        directive=(
            "Assert that a limit ACTUALLY FIRES: the specific status code, "
            "error body and retry header the implementation returns. Read the "
            "limit's real value out of the config or middleware and put the "
            "number in `test_data`; if you cannot find it, say so in `expected` "
            "and set `expected_verified: false` rather than guessing a "
            "plausible threshold. A case that says 'verify rate limiting works' "
            "without a number and a code is not runnable — do not emit it."
        ),
    ),
}


# Which categories each detected surface implies.
#
# `revoked_account` deliberately does NOT follow from a tRPC router alone:
# per SK-2705 the framework's protected-procedure wrapper already denies a
# soft-deleted user, and the finding was about the protocols that re-implement
# the check. Firing it on every tRPC change would be the boilerplate this
# module exists to avoid.
_CATEGORIES_BY_SURFACE: dict[str, tuple[str, ...]] = {
    "trpc_router": ("authz_matrix", "client_flag"),
    "raw_route": ("authz_matrix", "client_flag", "unauth_sweep", "revoked_account"),
    "stream_route": ("unauth_sweep", "revoked_account"),
    "auth_middleware": ("authz_matrix", "unauth_sweep", "revoked_account"),
    "upload_media": ("upload_abuse", "revoked_account"),
    "share_capability": ("authz_matrix", "client_flag"),
    "vendor_ai": ("abuse_cost", "revoked_account"),
}

# Render order — stable, so the same diff always produces the same prompt.
_CATEGORY_ORDER: tuple[str, ...] = (
    "authz_matrix",
    "client_flag",
    "unauth_sweep",
    "revoked_account",
    "upload_abuse",
    "abuse_cost",
)

# How many example paths to name per surface before truncating.
_MAX_PATHS_PER_SURFACE = 6


# ── Data types ───────────────────────────────────────────────────────────────


@dataclass
class SecurityContext:
    """Everything the prompt renderer needs to emit a security block."""

    # surface key -> the changed files that classified as it, in diff order
    surfaces: dict[str, list[str]] = field(default_factory=dict)
    # category keys that fired, in _CATEGORY_ORDER
    categories: list[str] = field(default_factory=list)
    # access-affecting parameter names found in added patch lines, with the
    # file each was seen in: [(param, path), ...]
    flags: list[tuple[str, str]] = field(default_factory=list)

    @property
    def entry_point_files(self) -> list[str]:
        """Changed files that are protected entry points, deduped in order.

        This is the row list for the deleted-account protocol matrix and the
        unauthenticated sweep: grounding those matrices in the files that
        actually changed is what keeps them from becoming a generic checklist
        of protocols the ticket never touched.
        """
        out: list[str] = []
        for surface in (
            "trpc_router",
            "raw_route",
            "stream_route",
            "upload_media",
            "vendor_ai",
        ):
            for path in self.surfaces.get(surface, []):
                if path not in out:
                    out.append(path)
        return out


# ── Public API ───────────────────────────────────────────────────────────────


def detect_security_surfaces(
    *,
    development_info: dict | None = None,
) -> SecurityContext | None:
    """Decide whether a ticket's diff touches a risky surface.

    Returns a populated :class:`SecurityContext` when at least one category
    fires, ``None`` otherwise — which is the common case and is why a CSS
    ticket gets no security section.

    Reads ONLY the diff. The ticket text is deliberately not consulted: a
    rule that needed the AC to mention security would fire on exactly the
    tickets that already get security attention, and stay silent on the ones
    that shipped SK-2702.
    """
    changes = _iter_changed_files(development_info)
    if not changes:
        return None

    ctx = SecurityContext()
    for path, patch in changes:
        for surface in _classify(path, patch):
            paths = ctx.surfaces.setdefault(surface, [])
            if path not in paths:
                paths.append(path)

    if not ctx.surfaces:
        return None

    # Access-affecting parameters, read from the added lines of the files
    # that are actually request entry points. A `role:` field in a database
    # model is not a client-controlled flag.
    flag_surfaces = {"trpc_router", "raw_route", "share_capability", "stream_route"}
    flag_paths = {
        path
        for surface, paths in ctx.surfaces.items()
        if surface in flag_surfaces
        for path in paths
    }
    seen_flags: set[tuple[str, str]] = set()
    for path, patch in changes:
        if path not in flag_paths or not patch:
            continue
        for param in _detect_access_params(patch):
            if (param, path) not in seen_flags:
                seen_flags.add((param, path))
                ctx.flags.append((param, path))

    fired: set[str] = set()
    for surface in ctx.surfaces:
        fired.update(_CATEGORIES_BY_SURFACE.get(surface, ()))

    # `client_flag` needs a named parameter or it is unrunnable.
    if not ctx.flags:
        fired.discard("client_flag")

    ctx.categories = [k for k in _CATEGORY_ORDER if k in fired]
    if not ctx.categories:
        return None
    return ctx


def merge_development_infos(infos) -> dict:
    """Flatten several tickets' ``development_info`` into one for detection.

    Multi-ticket plans emit ONE plan for the batch, so the security block is
    rendered once over the union of the batch's diffs — the same choice
    ``_merge_fanout_contexts`` makes for fan-out, and for the same reason:
    the block governs the SHAPE of a section the model emits once.
    :func:`detect_security_surfaces` already dedupes by path, so a file
    touched by two tickets is one row.
    """
    pull_requests: list = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        for pr in info.get("pull_requests") or []:
            pull_requests.append(pr)
    return {"pull_requests": pull_requests}


def render_security_guidance(ctx: SecurityContext) -> str:
    """Render the prompt block for a fired security detection.

    Names the surfaces that fired with their real paths, emits ONLY the
    categories those surfaces imply, and states the non-collapse and
    API-level rules that the plan-wide dedup rules would otherwise override.
    """
    lines: list[str] = []
    lines.append("")
    lines.append("━" * 69)
    lines.append("🔐 SECURITY NEGATIVE TESTS — TRIGGERED BY THE DIFF, NOT THE AC")
    lines.append("━" * 69)
    lines.append("")
    lines.append(
        "This ticket's diff touches a surface where authorization, account "
        "state, uploaded content or vendor spend can be attacked. Acceptance "
        "criteria almost never mention these, because an AC describes the "
        "intended user. **The rule that you may only test what the ticket "
        "explicitly mentions DOES NOT APPLY to this section** — the diff is the "
        "requirement here, and a case grounded in a changed route is grounded "
        "whether or not an AC named it."
    )
    lines.append("")
    lines.append("**Worked example — DO NOT REPRODUCE THIS FAILURE:**")
    lines.append(
        "  SK-2702. The public `shareFlowRecipient.authenticate` procedure "
        "accepted a caller-controlled `preview` boolean that reached "
        "`ShareFlowService.lookupLink`, where a draft was treated as active "
        "without checking that the caller was the draft's sender. Anyone who "
        "obtained a draft UUID could mint a guest session and read an unsent "
        "estimate, PDF or audio walkthrough. Every plan written for that "
        "procedure tested the sender previewing their own draft — the AC's "
        "claim — and passed. The deny paths had no cases at all."
    )
    lines.append("")

    lines.append("**Detected signals for THIS ticket:**")
    for surface in _SURFACE_PATH_PATTERNS:
        paths = ctx.surfaces.get(surface)
        if not paths:
            continue
        preview = ", ".join(paths[:_MAX_PATHS_PER_SURFACE])
        if len(paths) > _MAX_PATHS_PER_SURFACE:
            preview += f" (+{len(paths) - _MAX_PATHS_PER_SURFACE} more)"
        lines.append(f"  - {SURFACE_LABELS[surface]}: {preview}")
    if ctx.flags:
        lines.append("  - Access-affecting request parameters added or changed:")
        for param, path in ctx.flags[:10]:
            lines.append(f"      • `{param}` in {path}")
        if len(ctx.flags) > 10:
            lines.append(f"      • (+{len(ctx.flags) - 10} more)")
    lines.append("")

    lines.append(
        "**Emit these cases into the `security_negative_tests` section.** Only "
        "the categories below fired. Do NOT add security cases for categories "
        "that are not listed — an untouched surface gets no case."
    )
    lines.append("")

    for position, key in enumerate(ctx.categories, start=1):
        category = SECURITY_CATEGORIES[key]
        lines.append(f"**{position}. {category.title}**")
        lines.append(f"  _Why:_ {category.why}")
        lines.append("  _Required cases — one case each, never merged:_")
        for cell in category.cells:
            lines.append(f"      • {cell}")
        lines.append(f"  _How:_ {category.directive}")
        lines.append(f"  _Set `security_category: \"{category.key}\"` on each._")
        lines.append("")

    if "revoked_account" in ctx.categories or "unauth_sweep" in ctx.categories:
        entry_points = ctx.entry_point_files
        if entry_points:
            lines.append(
                "**The matrix rows for this ticket** — one case per entry point "
                "below, per the categories above. These are the files that "
                "changed; they are the whole matrix, and inventing a protocol "
                "this diff does not touch is as wrong as omitting one it does:"
            )
            for path in entry_points[:12]:
                lines.append(f"      • {path}")
            if len(entry_points) > 12:
                lines.append(f"      • (+{len(entry_points) - 12} more)")
            lines.append("")

    lines.append("**REQUIRED SHAPE — every case in this section:**")
    lines.append("")
    lines.append(
        "1. **One principal per case. One protocol per case.** Do NOT collapse "
        "\"anonymous, another user, the owner\" into a single case with a table "
        "of principals, and do NOT collapse a protocol matrix into one case "
        "that lists routes. The AVOID REDUNDANCY, PARAMETERIZE IDENTICAL TEST "
        "CASES and USE TABLES rules above are OVERRIDDEN here, and this "
        "exception is the entire point of the section: these cells have "
        "identical steps and different outcomes, so a merged case reports one "
        "verdict for three independent controls and passes while two of them "
        "are broken. Set `persona` on every case."
    )
    lines.append("")
    lines.append(
        "2. **API-level, not UI.** Set `surface: \"backend_http\"` on every "
        "case. These are run with curl, Postman or a Playwright request "
        "context — never through the app UI or the simulator, which cannot "
        "send an absent token or a foreign UUID. No step may imply a click, a "
        "screen or a screenshot."
    )
    lines.append("")
    lines.append(
        "3. **State the concrete request** in the `request` object: `method`, "
        "`route` (the real path from the diff, with a placeholder for each "
        "path parameter), `params` (query/body you are sending, including the "
        "attack value), `auth` (exactly what is in the Authorization header — "
        "`\"none\"` when the case is deliberately unauthenticated), and "
        "`protocol`. A reader must be able to build the request from this "
        "field alone. Mirror the same principal in `credentials`."
    )
    lines.append("")
    lines.append(
        "4. **Grounding is unchanged.** Every expected result either comes out "
        "of the implementation — `expected_verified: true` plus the exact "
        "`<file>:<line>` in `expected_source` — or is labelled an assumption "
        "with `expected_verified: false` and phrasing that says so "
        "(\"unverified — assumption: …\"). Status codes are the trap here: "
        "read the handler, do not infer 401 vs 403 from convention, and where "
        "you could not find the deny path at all, say that in `expected` "
        "rather than asserting a plausible code."
    )
    lines.append("")
    lines.append(
        "5. **Never invent a route, parameter or limit.** Every route you name "
        "must appear in the detected signals above or in the diff; every "
        "parameter must be one the diff actually accepts; every threshold must "
        "be read from config or middleware. If a control looks absent — no "
        "ownership check on the changed procedure, no active-account lookup on "
        "a protected route — that is a finding, not a test: put it in "
        "`risks_and_gaps` with the `<file>:<line>` where you expected it, AND "
        "still write the case that would catch it."
    )
    lines.append("")
    lines.append(
        "**Budget:** these cases are additive — they do not come out of the "
        "happy-path, edge-case or integration budgets, and they do not replace "
        "the functional coverage of the ticket. Expect roughly 3-6 cases per "
        "fired category. If a category's matrix would exceed 12 cases, cover "
        "every distinct protocol and principal once and say in "
        "`risks_and_gaps` which repetitions you dropped."
    )
    lines.append("")
    return "\n".join(lines)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _iter_changed_files(development_info: dict | None) -> list[tuple[str, str]]:
    """Every (path, added-lines) pair across all linked PRs, deduped by path.

    The patch is reduced to its added lines so a pattern can't match on a
    line the diff REMOVED — a route that was just deleted is not a surface
    this ticket exposes.
    """
    if not development_info:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for pr in development_info.get("pull_requests") or []:
        for change in (pr or {}).get("files_changed") or []:
            if not isinstance(change, dict):
                continue
            filename = change.get("filename")
            if not isinstance(filename, str) or not filename or filename in seen:
                continue
            if (change.get("status") or "").lower() == "removed":
                continue
            seen.add(filename)
            out.append((filename, _added_lines(change.get("patch"))))
    return out


def _added_lines(patch: str | None) -> str:
    if not isinstance(patch, str) or not patch:
        return ""
    return "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def _classify(path: str, added: str) -> list[str]:
    """Which surfaces a single changed file belongs to. May be several."""
    surfaces: list[str] = []
    probe = path if path.startswith("/") else "/" + path
    for surface, patterns in _SURFACE_PATH_PATTERNS.items():
        if any(re.search(p, probe, re.IGNORECASE) for p in patterns):
            surfaces.append(surface)
    if added:
        for surface, patterns in _SURFACE_PATCH_PATTERNS.items():
            if surface in surfaces:
                continue
            if any(re.search(p, added, re.IGNORECASE) for p in patterns):
                surfaces.append(surface)
    return surfaces


def _detect_access_params(added: str) -> list[str]:
    """Access-affecting parameter names declared in added lines.

    Order follows ``ACCESS_AFFECTING_PARAMS`` so the prompt is deterministic
    for a given diff.
    """
    found: list[str] = []
    for name in ACCESS_AFFECTING_PARAMS:
        escaped = re.escape(name)
        for template in _PARAM_CONTEXT_TEMPLATES:
            pattern = template.replace("{name}", escaped)
            if re.search(pattern, added, re.IGNORECASE):
                found.append(name)
                break
    return found


# ── Post-generation audit ────────────────────────────────────────────────────

# Words that name a principal in a case title or step. Used to catch a case
# that claims more than one — the collapse this section exists to prevent.
_PERSONA_MENTIONS: dict[str, tuple[str, ...]] = {
    "anonymous": (r"\banonymous\b", r"\bunauthenticated\b", r"\bno token\b", r"\blogged[- ]out\b"),
    "other_user": (r"\banother (?:authenticated )?user\b", r"\bdifferent user\b",
                   r"\bnon-?owner\b", r"\bsecond user\b", r"\bother account\b"),
    "owner": (r"\bthe owner\b", r"\bowning user\b", r"\bthe sender\b", r"\brecord owner\b"),
    "deleted_user": (r"\bsoft-?deleted\b", r"\bdeleted (?:user|account)\b",
                     r"\bdisabled account\b", r"\bdeactivated\b"),
    "guest_capability": (r"\bguest session\b", r"\bcapability link\b", r"\bshare link holder\b"),
}


def audit_persona_split(test_plan) -> list[dict]:
    """Flag security cases that collapsed more than one principal into one.

    This is the failure this whole section is shaped against, and it is the
    same failure the plan has had elsewhere twice: a compound AC line tested
    by one case, and a regression line asserting three share options at once.
    A case titled "Anonymous and non-owner callers are denied" reports ONE
    verdict for TWO independent controls, so it passes while one of them is
    wide open.

    Reports, never corrects — the same contract as
    :func:`copy_only.audit_plan_shape` and
    :func:`data_provisioning.flag_unactionable_data_asks`. Splitting a
    collapsed case means inventing the second case's expected result, which
    is precisely the fabrication the grounding rules forbid. The caller puts
    the result in ``source_provenance`` so the reviewer sees it.

    Returns one entry per offending case:
    ``{"case_id", "title", "declared_persona", "also_mentions"}``.
    """
    cases = getattr(test_plan, "security_negative_tests", None) or []
    findings: list[dict] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        declared = case.get("persona")
        blob = " ".join(
            [str(case.get("title") or "")]
            + [str(s) for s in (case.get("steps") or []) if isinstance(s, str)]
        )
        if not blob.strip():
            continue
        mentioned = {
            persona
            for persona, patterns in _PERSONA_MENTIONS.items()
            if any(re.search(p, blob, re.IGNORECASE) for p in patterns)
        }
        # The owner is legitimately named in a non-owner case ("a user who is
        # not the owner of the record"), so it only counts as a second
        # principal when the case did not declare it and names it on its own.
        extra = mentioned - {declared}
        if declared != "owner":
            extra.discard("owner")
        if not extra:
            continue
        findings.append(
            {
                "case_id": f"security_negative_tests:{index}",
                "title": case.get("title") or "",
                "declared_persona": declared,
                "also_mentions": sorted(extra),
            }
        )
    return findings
