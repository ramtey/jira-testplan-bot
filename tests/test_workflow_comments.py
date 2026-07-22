"""
Tests for the QA workflow comment builders used by the SK pass/fail-back
actions: `_build_qa_pass_adf`, `_build_qa_fail_adf`, and the URL/env
normalizers they depend on.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.jira_client import (
    ImageAttachment,
    JiraClient,
    QA_FAIL_MARKER,
    QA_PASS_MARKER,
    _build_mentions_paragraph,
    _build_qa_fail_adf,
    _build_qa_pass_adf,
    _normalize_environments,
    _normalize_url_list,
)


def _marker_text(adf_doc: dict) -> str:
    """Concatenate text nodes in the first paragraph of an ADF doc."""
    first = adf_doc["content"][0]
    return "".join(
        node.get("text", "") for node in first.get("content", [])
        if node.get("type") == "text"
    )


# ---------- _normalize_environments ----------

def test_normalize_environments_dedups_case_insensitively_and_preserves_order():
    assert _normalize_environments(["Integ", "staging", "INTEG"]) == ["Integ", "staging"]


def test_normalize_environments_strips_blank_and_non_strings():
    assert _normalize_environments(["  Integ  ", "", None, 42, "Staging"]) == ["Integ", "Staging"]


def test_normalize_environments_handles_none_and_empty():
    assert _normalize_environments(None) == []
    assert _normalize_environments([]) == []


# ---------- _normalize_url_list ----------

def test_normalize_url_list_dedups_and_strips():
    urls = ["https://x.com/a", "  https://x.com/a ", "https://x.com/b", "", None]
    assert _normalize_url_list(urls) == ["https://x.com/a", "https://x.com/b"]


def test_normalize_url_list_handles_none():
    assert _normalize_url_list(None) == []


# ---------- _build_qa_pass_adf ----------

def test_build_qa_pass_adf_returns_none_when_all_fields_empty():
    assert _build_qa_pass_adf(None, None, None) is None
    assert _build_qa_pass_adf([], "  ", []) is None


def test_build_qa_pass_adf_marker_only_when_only_envs():
    doc = _build_qa_pass_adf(None, None, ["Integ", "Staging"])
    assert doc is not None
    assert _marker_text(doc) == QA_PASS_MARKER.replace(
        "QA Passed", "QA Passed (Integ + Staging)"
    )
    # Only the marker paragraph — no Loom paragraph, no expand block.
    assert len(doc["content"]) == 1


def test_build_qa_pass_adf_marker_unchanged_without_envs():
    doc = _build_qa_pass_adf(["https://loom.com/x"], None, None)
    assert doc is not None
    assert _marker_text(doc) == QA_PASS_MARKER


def test_build_qa_pass_adf_renders_loom_link():
    doc = _build_qa_pass_adf(["https://loom.com/abc"], None, None)
    assert doc is not None
    loom_para = doc["content"][1]
    assert loom_para["type"] == "paragraph"
    link_node = next(
        n for n in loom_para["content"] if n.get("marks")
    )
    assert link_node["text"] == "https://loom.com/abc"
    assert link_node["marks"][0] == {"type": "link", "attrs": {"href": "https://loom.com/abc"}}


def test_build_qa_pass_adf_renders_multiple_loom_links_in_order():
    doc = _build_qa_pass_adf(
        ["https://loom.com/a", "https://loom.com/b"], None, None
    )
    assert doc is not None
    loom_paragraphs = [
        p for p in doc["content"]
        if p.get("type") == "paragraph"
        and p.get("content") and p["content"][0].get("text", "").startswith("📹")
    ]
    assert len(loom_paragraphs) == 2
    hrefs = [p["content"][1]["marks"][0]["attrs"]["href"] for p in loom_paragraphs]
    assert hrefs == ["https://loom.com/a", "https://loom.com/b"]


def test_build_qa_pass_adf_dedups_loom_urls_and_drops_blanks():
    doc = _build_qa_pass_adf(
        ["https://loom.com/a", "  ", "https://loom.com/a", ""],
        None,
        None,
    )
    assert doc is not None
    loom_paragraphs = [
        p for p in doc["content"]
        if p.get("type") == "paragraph"
        and p.get("content") and p["content"][0].get("text", "").startswith("📹")
    ]
    assert len(loom_paragraphs) == 1


def test_build_qa_pass_adf_summary_renders_inline():
    doc = _build_qa_pass_adf(None, "Tested the **happy path**", None)
    assert doc is not None
    # Summary is appended as inline content — no `expand` wrapper — so any
    # URLs in the summary stay one-click clickable.
    assert not any(node["type"] == "expand" for node in doc["content"])
    flattened = str(doc["content"])
    assert "happy path" in flattened
    assert "strong" in flattened


def _image_paragraphs(doc):
    return [
        p for p in doc["content"]
        if p.get("type") == "paragraph"
        and p.get("content")
        and p["content"][0].get("text", "").startswith("📷")
    ]


def _media_single_nodes(doc):
    return [n for n in doc["content"] if n.get("type") == "mediaSingle"]


def test_build_qa_pass_adf_renders_image_callouts_above_summary():
    # Screenshots supplied without a resolved media UUID fall back to
    # plain `📷 <filename>` callout paragraphs above the inline summary —
    # reviewers still see which screenshots landed, and the Attachments
    # panel renders the actual image below the comment.
    doc = _build_qa_pass_adf(
        None,
        "Some test summary",
        None,
        None,
        [("a.png", "https://jira.example/a"), ("b.png", "https://jira.example/b")],
    )
    assert doc is not None
    paragraphs = _image_paragraphs(doc)
    assert len(paragraphs) == 2
    texts = [p["content"][0]["text"] for p in paragraphs]
    assert texts == ["📷 a.png", "📷 b.png"]
    # Each callout is a single plain-text node with no link marks.
    for p in paragraphs:
        assert len(p["content"]) == 1
        assert "marks" not in p["content"][0]
    # Callouts appear before the summary paragraph.
    callout_indices = [doc["content"].index(p) for p in paragraphs]
    summary_index = next(
        i for i, node in enumerate(doc["content"])
        if node.get("type") == "paragraph"
        and any(
            "Some test summary" in child.get("text", "")
            for child in node.get("content", [])
        )
    )
    assert max(callout_indices) < summary_index


def test_build_qa_pass_adf_renders_media_single_when_media_id_present():
    # With a resolved media-services UUID, screenshots render inline
    # via a `mediaSingle` node — this is the primary happy path.
    doc = _build_qa_pass_adf(
        None,
        None,
        None,
        None,
        [
            ImageAttachment("a.png", "https://jira.example/a", "uuid-a"),
            ImageAttachment("b.png", "https://jira.example/b", "uuid-b"),
        ],
    )
    assert doc is not None
    media = _media_single_nodes(doc)
    assert len(media) == 2
    for node, expected_uuid in zip(media, ["uuid-a", "uuid-b"]):
        assert node["attrs"] == {"layout": "center"}
        assert len(node["content"]) == 1
        inner = node["content"][0]
        assert inner["type"] == "media"
        assert inner["attrs"] == {
            "type": "file",
            "id": expected_uuid,
            "collection": "",
        }
    # No plain-text callouts when every image resolved to a UUID.
    assert _image_paragraphs(doc) == []


def test_build_qa_pass_adf_mixes_media_and_text_when_partially_resolved():
    # If UUID resolution fails for one screenshot, only that one falls
    # back to the text callout — the others still render inline.
    doc = _build_qa_pass_adf(
        None,
        None,
        None,
        None,
        [
            ImageAttachment("a.png", "https://jira.example/a", "uuid-a"),
            ImageAttachment("b.png", "https://jira.example/b", None),
        ],
    )
    assert doc is not None
    media = _media_single_nodes(doc)
    text_callouts = _image_paragraphs(doc)
    assert len(media) == 1
    assert media[0]["content"][0]["attrs"]["id"] == "uuid-a"
    assert len(text_callouts) == 1
    assert text_callouts[0]["content"][0]["text"] == "📷 b.png"


def test_build_qa_pass_adf_images_alone_create_a_comment():
    # A screenshot is a meaningful artifact even without loom/summary/envs.
    doc = _build_qa_pass_adf(None, None, None, None, [("a.png", "https://jira.example/a")])
    assert doc is not None
    paragraphs = _image_paragraphs(doc)
    assert len(paragraphs) == 1


def test_build_qa_pass_adf_dedups_images_and_drops_blanks():
    doc = _build_qa_pass_adf(
        ["https://loom.com/x"],
        None,
        None,
        None,
        [
            ("a.png", "https://jira.example/a"),
            ("a.png", "  "),
            ("a.png", "https://jira.example/a"),
            ("", "https://jira.example/empty-name"),
        ],
    )
    assert doc is not None
    paragraphs = _image_paragraphs(doc)
    assert len(paragraphs) == 1
    assert paragraphs[0]["content"][0]["text"] == "📷 a.png"


def test_build_qa_pass_adf_loom_then_images_then_summary_then_mentions():
    # Screenshot arrives with a resolved media UUID — the mediaSingle
    # node sits between the Loom link and the inline summary paragraph,
    # and mentions bring up the rear.
    doc = _build_qa_pass_adf(
        ["https://loom.com/x"],
        "Summary text",
        ["Integ"],
        ["acct-1"],
        [ImageAttachment("a.png", "https://jira.example/a", "uuid-a")],
    )
    assert doc is not None
    types = [node["type"] for node in doc["content"]]
    assert types[0] == "paragraph"  # marker
    # Summary is inlined — no expand wrapper.
    assert "expand" not in types
    loom_idx = next(
        i for i, p in enumerate(doc["content"])
        if p["type"] == "paragraph"
        and p.get("content") and p["content"][0].get("text", "").startswith("📹")
    )
    media_idx = next(
        i for i, n in enumerate(doc["content"]) if n["type"] == "mediaSingle"
    )
    summary_idx = next(
        i for i, p in enumerate(doc["content"])
        if p.get("type") == "paragraph"
        and any(
            "Summary text" in child.get("text", "")
            for child in p.get("content", [])
        )
    )
    mention_idx = next(
        i for i, p in enumerate(doc["content"])
        if p["type"] == "paragraph"
        and any(n.get("type") == "mention" for n in p.get("content", []))
    )
    assert loom_idx < media_idx < summary_idx < mention_idx


# ---------- _build_qa_fail_adf ----------

def test_build_qa_fail_adf_returns_none_without_reason():
    # Reason is the load-bearing field — without it nothing should post.
    assert _build_qa_fail_adf(
        None, ["https://loom.com/x"], [("a.png", "https://jira.example/a")]
    ) is None
    assert _build_qa_fail_adf(
        "   ", ["https://loom.com/x"], [("a.png", "https://jira.example/a")]
    ) is None


def test_build_qa_fail_adf_minimum_is_marker_plus_reason():
    doc = _build_qa_fail_adf("Login broken on staging", None, None)
    assert doc is not None
    assert _marker_text(doc) == QA_FAIL_MARKER
    # Marker paragraph + at least one reason paragraph; nothing else.
    assert len(doc["content"]) == 2
    reason_para = doc["content"][1]
    reason_text = "".join(
        n.get("text", "") for n in reason_para.get("content", [])
    )
    assert reason_text == "Login broken on staging"


def test_build_qa_fail_adf_reason_is_above_fold_not_in_expand():
    # Devs need to see WHY without expanding anything — keep it inline.
    doc = _build_qa_fail_adf("Repro: click login", None, None)
    assert doc is not None
    assert all(node["type"] != "expand" for node in doc["content"])


def test_build_qa_fail_adf_renders_markdown_in_reason():
    doc = _build_qa_fail_adf("- step one\n- step two", None, None)
    assert doc is not None
    # Marker paragraph at index 0, then a bulletList from markdown_to_adf.
    bullet = doc["content"][1]
    assert bullet["type"] == "bulletList"
    assert len(bullet["content"]) == 2


def test_build_qa_fail_adf_appends_loom_then_image_callouts_in_order():
    doc = _build_qa_fail_adf(
        "Reason here",
        ["https://loom.com/x"],
        [("a.png", "https://jira.example/a"), ("b.png", "https://jira.example/b")],
    )
    assert doc is not None
    # Expected order after marker + reason paragraph: Loom paragraph,
    # then two plain-text `📷 <filename>` callout paragraphs (no link
    # marks — see _build_attachment_label_paragraph for rationale).
    after_reason = doc["content"][2:]
    assert after_reason[0]["content"][0]["text"].startswith("📹 Loom")
    assert after_reason[1]["content"][0]["text"] == "📷 a.png"
    assert "marks" not in after_reason[1]["content"][0]
    assert after_reason[2]["content"][0]["text"] == "📷 b.png"
    assert "marks" not in after_reason[2]["content"][0]


def test_build_qa_fail_adf_renders_media_single_when_media_id_present():
    # Fail-back comments render screenshots inline via mediaSingle just
    # like pass comments do — same recipe, same fallback rules.
    doc = _build_qa_fail_adf(
        "Login broken",
        None,
        [ImageAttachment("a.png", "https://jira.example/a", "uuid-a")],
    )
    assert doc is not None
    media = _media_single_nodes(doc)
    assert len(media) == 1
    inner = media[0]["content"][0]
    assert inner["type"] == "media"
    assert inner["attrs"]["id"] == "uuid-a"
    # No plain-text callout when the UUID resolved.
    assert _image_paragraphs(doc) == []


def test_build_qa_pass_adf_dedups_mixed_tuple_and_namedtuple_by_url():
    # Callers may pass a mix of legacy 2-tuples, 3-tuples, and
    # ImageAttachment NamedTuples — dedup runs on URL regardless of
    # shape so a caller merging fresh uploads with walkthrough entries
    # never emits the same screenshot twice.
    doc = _build_qa_pass_adf(
        ["https://loom.com/x"],
        None,
        None,
        None,
        [
            ("a.png", "https://jira.example/a"),
            ("a.png", "https://jira.example/a", "uuid-a"),
            ImageAttachment("a.png", "https://jira.example/a", "uuid-a"),
        ],
    )
    assert doc is not None
    # First occurrence wins — it's the 2-tuple (media_id=None), so we
    # get a text callout, not a mediaSingle node.
    media = _media_single_nodes(doc)
    text_callouts = _image_paragraphs(doc)
    assert len(media) + len(text_callouts) == 1


def test_build_qa_fail_adf_dedups_images_and_drops_blanks():
    doc = _build_qa_fail_adf(
        "Reason",
        None,
        [
            ("a.png", "https://jira.example/a"),
            ("a.png", "  "),
            ("a.png", "https://jira.example/a"),
            ("", "https://jira.example/empty-name"),
        ],
    )
    assert doc is not None
    paragraphs = [
        p for p in doc["content"]
        if p.get("type") == "paragraph"
        and p.get("content") and p["content"][0].get("text", "").startswith("📷")
    ]
    assert len(paragraphs) == 1
    assert paragraphs[0]["content"][0]["text"] == "📷 a.png"


# ---------- _build_mentions_paragraph ----------

def test_build_mentions_paragraph_returns_none_when_empty():
    assert _build_mentions_paragraph(None) is None
    assert _build_mentions_paragraph([]) is None
    assert _build_mentions_paragraph(["", "  "]) is None


def test_build_mentions_paragraph_emits_mention_nodes_with_cc_prefix():
    para = _build_mentions_paragraph(["acct-1", "acct-2"])
    assert para is not None
    assert para["type"] == "paragraph"
    nodes = para["content"]
    assert nodes[0] == {"type": "text", "text": "cc: "}
    mention_nodes = [n for n in nodes if n.get("type") == "mention"]
    assert [n["attrs"]["id"] for n in mention_nodes] == ["acct-1", "acct-2"]


def test_build_mentions_paragraph_dedupes_account_ids():
    para = _build_mentions_paragraph(["acct-1", "acct-1", " acct-2 ", "acct-2"])
    assert para is not None
    mention_ids = [n["attrs"]["id"] for n in para["content"] if n.get("type") == "mention"]
    assert mention_ids == ["acct-1", "acct-2"]


# ---------- mentions integrated into pass / fail comments ----------

def test_build_qa_pass_adf_appends_mentions_at_end():
    doc = _build_qa_pass_adf(
        ["https://loom.com/x"],
        None,
        ["Integ"],
        ["acct-1", "acct-2"],
    )
    assert doc is not None
    last = doc["content"][-1]
    assert last["type"] == "paragraph"
    mention_ids = [n["attrs"]["id"] for n in last["content"] if n.get("type") == "mention"]
    assert mention_ids == ["acct-1", "acct-2"]


def test_build_qa_pass_adf_mentions_alone_do_not_create_a_comment():
    # No loom/summary/envs means there's nothing meaningful to post —
    # mentions on an empty comment shouldn't trigger a notification.
    assert _build_qa_pass_adf(None, None, None, ["acct-1"]) is None


def test_build_qa_fail_adf_appends_mentions_after_attachments():
    doc = _build_qa_fail_adf(
        "Login broken",
        ["https://loom.com/x"],
        [("a.png", "https://jira.example/a")],
        ["acct-1"],
    )
    assert doc is not None
    last = doc["content"][-1]
    assert last["type"] == "paragraph"
    assert any(n.get("type") == "mention" and n["attrs"]["id"] == "acct-1" for n in last["content"])


def test_build_qa_fail_adf_mentions_alone_do_not_create_a_comment():
    # Reason is still required — pinging people with no explanation isn't useful.
    assert _build_qa_fail_adf(None, None, None, ["acct-1"]) is None
    assert _build_qa_fail_adf("   ", None, None, ["acct-1"]) is None


def test_build_qa_pass_adf_no_mentions_paragraph_when_list_empty():
    doc = _build_qa_pass_adf(["https://loom.com/x"], None, None, None)
    assert doc is not None
    assert all(
        not any(node.get("type") == "mention" for node in para.get("content", []))
        for para in doc["content"]
    )


# ---------- JiraClient.resolve_media_id ----------

def _redirect_response(location: str, status_code: int = 303):
    """Build a MagicMock httpx.Response mimicking a redirect."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Location": location}
    return resp


@pytest.mark.asyncio
async def test_resolve_media_id_extracts_uuid_from_303_location():
    jira = JiraClient()
    location = (
        "https://api.media.atlassian.com/file/"
        "abcdef12-3456-7890-abcd-ef1234567890/binary?token=jwt&client=uuid"
    )
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_redirect_response(location)
        )
        result = await jira.resolve_media_id("10001")
    assert result == "abcdef12-3456-7890-abcd-ef1234567890"


@pytest.mark.asyncio
async def test_resolve_media_id_handles_302_redirect():
    # Some Jira tenants return 302 instead of 303 — accept both.
    jira = JiraClient()
    location = "https://api.media.atlassian.com/file/deadbeef-0000-1111-2222-333333333333/binary"
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_redirect_response(location, status_code=302)
        )
        result = await jira.resolve_media_id("10002")
    assert result == "deadbeef-0000-1111-2222-333333333333"


@pytest.mark.asyncio
async def test_resolve_media_id_returns_none_on_unexpected_status():
    jira = JiraClient()
    resp = MagicMock()
    resp.status_code = 200  # Not a redirect — treat as failure.
    resp.headers = {}
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=resp
        )
        result = await jira.resolve_media_id("10003")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_media_id_returns_none_when_location_has_no_uuid():
    jira = JiraClient()
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=_redirect_response("https://example.com/no/uuid/here")
        )
        result = await jira.resolve_media_id("10004")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_media_id_returns_none_on_empty_id():
    jira = JiraClient()
    # No HTTP call should happen — the guard rejects empty ids up front.
    with patch("httpx.AsyncClient") as mock_client:
        result = await jira.resolve_media_id("")
        mock_client.assert_not_called()
    assert result is None


@pytest.mark.asyncio
async def test_enrich_attachments_with_media_ids_zips_uuids_into_refs():
    jira = JiraClient()
    uploaded = [
        {"id": "1", "filename": "a.png", "content": "https://jira.example/a"},
        {"id": "2", "filename": "b.png", "content": "https://jira.example/b"},
        # Missing content URL — should be dropped from the output.
        {"id": "3", "filename": "c.png", "content": ""},
    ]
    with patch.object(
        jira, "resolve_media_id", new=AsyncMock(side_effect=["uuid-a", "uuid-b"])
    ):
        result = await jira.enrich_attachments_with_media_ids(uploaded)
    assert result == [
        ImageAttachment("a.png", "https://jira.example/a", "uuid-a"),
        ImageAttachment("b.png", "https://jira.example/b", "uuid-b"),
    ]


# ---------- workflow_routes helpers ----------

def test_attachment_id_from_content_url_extracts_numeric_id():
    from src.app.workflow_routes import _attachment_id_from_content_url
    url = "https://acme.atlassian.net/rest/api/3/attachment/content/54321"
    assert _attachment_id_from_content_url(url) == "54321"


def test_attachment_id_from_content_url_handles_trailing_query():
    from src.app.workflow_routes import _attachment_id_from_content_url
    url = "https://acme.atlassian.net/rest/api/3/attachment/content/9876?token=x"
    assert _attachment_id_from_content_url(url) == "9876"


def test_attachment_id_from_content_url_returns_none_for_foreign_or_blank():
    from src.app.workflow_routes import _attachment_id_from_content_url
    assert _attachment_id_from_content_url("") is None
    assert _attachment_id_from_content_url("https://example.com/some/other/path") is None


# ---------- walkthrough_repository media_id round-trip ----------

def test_decode_screenshots_preserves_media_id():
    from src.app.repositories import walkthrough_repository

    row = MagicMock()
    row.screenshots = (
        '[{"filename": "a.png", "url": "https://jira.example/a",'
        ' "media_id": "uuid-a"},'
        '{"filename": "b.png", "url": "https://jira.example/b"}]'
    )
    result = walkthrough_repository.decode_screenshots(row)
    assert result == [
        {"filename": "a.png", "url": "https://jira.example/a", "media_id": "uuid-a"},
        {"filename": "b.png", "url": "https://jira.example/b", "media_id": None},
    ]


def test_decode_screenshots_ignores_blank_media_id():
    from src.app.repositories import walkthrough_repository

    row = MagicMock()
    row.screenshots = (
        '[{"filename": "a.png", "url": "https://jira.example/a", "media_id": "  "}]'
    )
    result = walkthrough_repository.decode_screenshots(row)
    assert result == [
        {"filename": "a.png", "url": "https://jira.example/a", "media_id": None}
    ]


@pytest.mark.asyncio
async def test_enrich_attachments_with_media_ids_tolerates_failed_lookup():
    # Attachment 2's UUID lookup returned None — its ImageAttachment
    # comes through with media_id=None so the comment builder can fall
    # back to a text callout.
    jira = JiraClient()
    uploaded = [
        {"id": "1", "filename": "a.png", "content": "https://jira.example/a"},
        {"id": "2", "filename": "b.png", "content": "https://jira.example/b"},
    ]
    with patch.object(
        jira, "resolve_media_id", new=AsyncMock(side_effect=["uuid-a", None])
    ):
        result = await jira.enrich_attachments_with_media_ids(uploaded)
    assert [(r.filename, r.media_id) for r in result] == [
        ("a.png", "uuid-a"),
        ("b.png", None),
    ]
