"""The diff-driven security trigger, and the shape of what it asks for.

These pin the two properties the rule lives or dies by:

* It fires on the surfaces that shipped SK-2702 through SK-2707, reading the
  diff and never the acceptance criteria — an AC describes the intended user,
  which is why none of those six bugs had a case.
* It stays silent otherwise. A rule that emits security boilerplate on a CSS
  ticket gets turned off, and then catches nothing at all.
"""

from types import SimpleNamespace

import pytest

from src.app.security_surfaces import (
    SECURITY_CATEGORIES,
    audit_persona_split,
    detect_security_surfaces,
    merge_development_infos,
    render_security_guidance,
)


def dev(*files):
    """Build a `development_info` from (path, patch) pairs."""
    return {
        "pull_requests": [
            {
                "files_changed": [
                    {"filename": path, "status": "modified", "patch": patch}
                    for path, patch in files
                ]
            }
        ]
    }


# ── The SK-2702 shape: a caller-controlled flag on a share procedure ─────────


SHARE_ROUTER = (
    "apps/server/src/api/router/share-flow-recipient.ts",
    "@@ -1,2 +1,5 @@\n"
    "+export const shareFlowRecipient = router({\n"
    "+  authenticate: publicProcedure\n"
    "+    .input(z.object({ linkId: z.string(), preview: z.boolean().optional() }))\n",
)


class TestClientControlledFlags:
    def test_names_the_flag_it_found(self):
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        assert ctx is not None
        assert ("preview", SHARE_ROUTER[0]) in ctx.flags
        assert "client_flag" in ctx.categories

    def test_flag_name_reaches_the_prompt(self):
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        block = render_security_guidance(ctx)
        # A case that cannot name the parameter cannot be run.
        assert "`preview`" in block

    def test_no_named_flag_means_no_flag_category(self):
        """The category is suppressed rather than emitted as a generic ask.

        "Audit the new parameters for access effects" is not a test case —
        nobody can run it and nobody can mark it.
        """
        ctx = detect_security_surfaces(
            development_info=dev(
                (
                    "apps/server/src/api/router/calculations.ts",
                    "@@ -1 +1,2 @@\n+  list: protectedProcedure.query(async ({ ctx }) => {\n",
                )
            )
        )
        assert ctx is not None
        assert "client_flag" not in ctx.categories
        assert "authz_matrix" in ctx.categories

    def test_a_model_field_is_not_a_client_flag(self):
        """`role:` in a database model is not a caller-controlled parameter."""
        ctx = detect_security_surfaces(
            development_info=dev(
                ("packages/db/src/models/user.ts", "@@ -1 +1,2 @@\n+  role: z.string(),\n")
            )
        )
        assert ctx is None or "client_flag" not in ctx.categories


# ── The SK-2705 shape: one protocol enforces, the others do not ──────────────


class TestProtocolMatrix:
    def test_raw_and_stream_routes_fire_revocation(self):
        ctx = detect_security_surfaces(
            development_info=dev(
                (
                    "apps/server/src/routes/tts-stream.ts",
                    "@@ -1 +1,3 @@\n+  app.get('/tts/stream', async (req, res) => {\n"
                    "+    const wss = new WebSocketServer({ noServer: true })\n",
                ),
                (
                    "apps/server/src/api/calculations/getCalculationPdf.ts",
                    "@@ -1 +1,2 @@\n+  const session = await getSession(req)\n",
                ),
            )
        )
        assert ctx is not None
        assert "revoked_account" in ctx.categories
        assert "unauth_sweep" in ctx.categories

    def test_matrix_rows_are_the_files_that_changed(self):
        """The matrix is grounded in the diff, not in a list of protocols.

        Enumerating protocols the ticket never touched is the boilerplate
        this module exists to avoid; omitting one it did touch is SK-2705.
        """
        ctx = detect_security_surfaces(
            development_info=dev(
                ("apps/server/src/routes/tts-stream.ts", "+app.get('/x', h)\n"),
                ("apps/server/src/routes/upload.ts", "+app.post('/u', h)\n"),
            )
        )
        block = render_security_guidance(ctx)
        assert "apps/server/src/routes/tts-stream.ts" in block
        assert "apps/server/src/routes/upload.ts" in block

    def test_a_plain_trpc_change_does_not_claim_a_revocation_gap(self):
        """tRPC's protected wrapper already denies a soft-deleted user.

        Firing revocation on every tRPC change would put an untestable
        boilerplate case on a large fraction of all tickets.
        """
        ctx = detect_security_surfaces(
            development_info=dev(
                (
                    "apps/server/src/api/router/calculations.ts",
                    "@@ -1 +1,2 @@\n+  list: protectedProcedure.query(h)\n",
                )
            )
        )
        assert ctx is not None
        assert "revoked_account" not in ctx.categories


# ── Upload and cost surfaces ────────────────────────────────────────────────


class TestOtherSurfaces:
    def test_upload_handler_fires_upload_abuse(self):
        ctx = detect_security_surfaces(
            development_info=dev(
                (
                    "apps/server/src/routes/profile-media.ts",
                    "@@ -1 +1,2 @@\n+  const upload = multer({ storage })\n",
                )
            )
        )
        assert ctx is not None
        assert "upload_abuse" in ctx.categories

    def test_served_headers_are_part_of_the_upload_ask(self):
        """The upload half is usually tested; the serving half never is."""
        ctx = detect_security_surfaces(
            development_info=dev(("apps/server/src/routes/upload.ts", "+multer()\n"))
        )
        block = render_security_guidance(ctx)
        assert "X-Content-Type-Options" in block
        assert "Cache-Control" in block

    def test_vendor_call_fires_cost(self):
        ctx = detect_security_surfaces(
            development_info=dev(
                (
                    "apps/server/src/services/llm/generate.ts",
                    "@@ -1 +1,2 @@\n+  const res = await openai.completions.create(req)\n",
                )
            )
        )
        assert ctx is not None
        assert "abuse_cost" in ctx.categories


# ── Silence is the default ──────────────────────────────────────────────────


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "path,patch",
        [
            ("apps/web/src/styles/theme.css", "+.btn { color: red; }\n"),
            ("apps/expo/src/components/Button.tsx", "+const Label = () => <Text/>\n"),
            ("README.md", "+## Setup\n"),
            ("packages/engine/src/calculators/buyerNetSheet.ts", "+  const t = a + b\n"),
        ],
    )
    def test_no_security_section_on_an_ordinary_ticket(self, path, patch):
        assert detect_security_surfaces(development_info=dev((path, patch))) is None

    def test_no_diff_means_no_detection(self):
        assert detect_security_surfaces(development_info=None) is None
        assert detect_security_surfaces(development_info={}) is None

    def test_a_deleted_route_is_not_a_surface_this_ticket_exposes(self):
        """Only ADDED lines classify. A route the diff removes is not one to test."""
        info = {
            "pull_requests": [
                {
                    "files_changed": [
                        {
                            "filename": "src/server/legacy.ts",
                            "status": "removed",
                            "patch": "@@ -1,2 +0,0 @@\n-app.get('/legacy', handler)\n",
                        }
                    ]
                }
            ]
        }
        assert detect_security_surfaces(development_info=info) is None


# ── The prompt block itself ─────────────────────────────────────────────────


class TestRenderedGuidance:
    def test_only_fired_categories_appear(self):
        ctx = detect_security_surfaces(
            development_info=dev(("apps/server/src/routes/upload.ts", "+multer()\n"))
        )
        block = render_security_guidance(ctx)
        assert SECURITY_CATEGORIES["upload_abuse"].title in block
        # Nothing in an upload diff justifies asking for rate-limit coverage.
        assert SECURITY_CATEGORIES["abuse_cost"].title not in block

    def test_overrides_the_ac_only_rule_explicitly(self):
        """Without this, the prompt's own top rule deletes the whole section.

        SYSTEM_PROMPT opens with "ONLY create test cases for features
        explicitly described in the ticket" — which describes every case
        here, since no AC mentions anonymous callers.
        """
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        block = render_security_guidance(ctx)
        assert "DOES NOT APPLY" in block

    def test_forbids_the_collapse_the_dedup_rules_would_force(self):
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        block = render_security_guidance(ctx)
        assert "OVERRIDDEN" in block
        assert "AVOID REDUNDANCY" in block

    def test_demands_an_api_level_surface(self):
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        block = render_security_guidance(ctx)
        assert 'surface: "backend_http"' in block

    def test_keeps_the_grounding_rule(self):
        ctx = detect_security_surfaces(development_info=dev(SHARE_ROUTER))
        block = render_security_guidance(ctx)
        assert "expected_verified" in block
        assert "expected_source" in block

    def test_is_deterministic_for_a_given_diff(self):
        info = dev(SHARE_ROUTER, ("apps/server/src/routes/upload.ts", "+multer()\n"))
        first = render_security_guidance(detect_security_surfaces(development_info=info))
        second = render_security_guidance(detect_security_surfaces(development_info=info))
        assert first == second


class TestMultiTicketMerge:
    def test_union_of_the_batch_diffs(self):
        a = dev(SHARE_ROUTER)
        b = dev(("apps/server/src/routes/upload.ts", "+multer()\n"))
        ctx = detect_security_surfaces(
            development_info=merge_development_infos([a, b, None])
        )
        assert ctx is not None
        assert "client_flag" in ctx.categories
        assert "upload_abuse" in ctx.categories


# ── The collapse guard ──────────────────────────────────────────────────────


class TestPersonaSplitAudit:
    def test_flags_a_case_claiming_two_principals(self):
        plan = SimpleNamespace(
            security_negative_tests=[
                {
                    "title": "Anonymous and non-owner callers are denied",
                    "persona": "anonymous",
                    "steps": ["Call as anonymous", "Call as another user"],
                }
            ]
        )
        found = audit_persona_split(plan)
        assert len(found) == 1
        assert found[0]["case_id"] == "security_negative_tests:0"
        assert "other_user" in found[0]["also_mentions"]

    def test_a_single_principal_case_is_clean(self):
        plan = SimpleNamespace(
            security_negative_tests=[
                {
                    "title": "Anonymous caller cannot activate a draft",
                    "persona": "anonymous",
                    "steps": ["Send with no Authorization header"],
                }
            ]
        )
        assert audit_persona_split(plan) == []

    def test_naming_the_owner_to_define_a_non_owner_is_not_a_collapse(self):
        """"a user who is not the owner" is one principal, described precisely."""
        plan = SimpleNamespace(
            security_negative_tests=[
                {
                    "title": "A user who is not the owner of the record is denied",
                    "persona": "other_user",
                    "steps": ["Use a second test user's token"],
                }
            ]
        )
        assert audit_persona_split(plan) == []

    def test_no_security_section_means_nothing_to_audit(self):
        assert audit_persona_split(SimpleNamespace()) == []
        assert audit_persona_split(SimpleNamespace(security_negative_tests=[])) == []


class TestQuarantineExclusion:
    """A security case is gradeable even when its status code is unverified.

    Quarantining pulls a case out of the checklist when nothing about it traces
    to code, because nobody can tell whether a failure is a defect. That
    reasoning does not reach this section: what SHOULD happen is not in doubt.
    """

    def test_an_ungrounded_security_case_stays_in_its_section(self):
        from src.app.services.test_plan_generator import quarantine_ungrounded_cases

        plan = SimpleNamespace(
            happy_path=[
                {
                    "title": "speculative UI case",
                    "needs_manual_verification": True,
                    "expected_verified": False,
                }
            ],
            edge_cases=[],
            integration_tests=[],
            security_negative_tests=[
                {
                    "title": "Anonymous caller cannot read another sender's draft",
                    "persona": "anonymous",
                    "needs_manual_verification": True,
                    "expected_verified": False,
                }
            ],
        )
        quarantined = quarantine_ungrounded_cases(plan)
        # The speculative happy-path case moves out, as it always has.
        assert len(quarantined) == 1
        assert plan.happy_path == []
        # The security case stays where a tester will run it.
        assert len(plan.security_negative_tests) == 1
