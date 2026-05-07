"""
Tests for the QA workflow comment builders used by the SK pass/fail-back
actions: `_build_qa_pass_adf`, `_build_qa_fail_adf`, and the URL/env
normalizers they depend on.
"""

from src.app.jira_client import (
    QA_FAIL_MARKER,
    QA_PASS_EXPAND_TITLE,
    QA_PASS_MARKER,
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
    assert _build_qa_pass_adf("", "  ", []) is None


def test_build_qa_pass_adf_marker_only_when_only_envs():
    doc = _build_qa_pass_adf(None, None, ["Integ", "Staging"])
    assert doc is not None
    assert _marker_text(doc) == QA_PASS_MARKER.replace(
        "QA Passed", "QA Passed (Integ + Staging)"
    )
    # Only the marker paragraph — no Loom paragraph, no expand block.
    assert len(doc["content"]) == 1


def test_build_qa_pass_adf_marker_unchanged_without_envs():
    doc = _build_qa_pass_adf("https://loom.com/x", None, None)
    assert doc is not None
    assert _marker_text(doc) == QA_PASS_MARKER


def test_build_qa_pass_adf_renders_loom_link():
    doc = _build_qa_pass_adf("https://loom.com/abc", None, None)
    assert doc is not None
    loom_para = doc["content"][1]
    assert loom_para["type"] == "paragraph"
    link_node = next(
        n for n in loom_para["content"] if n.get("marks")
    )
    assert link_node["text"] == "https://loom.com/abc"
    assert link_node["marks"][0] == {"type": "link", "attrs": {"href": "https://loom.com/abc"}}


def test_build_qa_pass_adf_summary_goes_into_expand():
    doc = _build_qa_pass_adf(None, "Tested the **happy path**", None)
    assert doc is not None
    expand = doc["content"][-1]
    assert expand["type"] == "expand"
    assert expand["attrs"]["title"] == QA_PASS_EXPAND_TITLE
    # The bold marker should have survived markdown_to_adf.
    flattened = str(expand["content"])
    assert "happy path" in flattened
    assert "strong" in flattened


# ---------- _build_qa_fail_adf ----------

def test_build_qa_fail_adf_returns_none_without_reason():
    # Reason is the load-bearing field — without it nothing should post.
    assert _build_qa_fail_adf(None, "https://loom.com/x", ["https://i.imgur.com/a.png"]) is None
    assert _build_qa_fail_adf("   ", "https://loom.com/x", ["https://i.imgur.com/a.png"]) is None


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


def test_build_qa_fail_adf_appends_loom_then_images_in_order():
    doc = _build_qa_fail_adf(
        "Reason here",
        "https://loom.com/x",
        ["https://i.imgur.com/a.png", "https://i.imgur.com/b.png"],
    )
    assert doc is not None
    # Expected order after marker + reason paragraph: Loom paragraph, then images.
    paragraphs_after_reason = doc["content"][2:]
    assert paragraphs_after_reason[0]["content"][0]["text"].startswith("📹 Loom")
    assert paragraphs_after_reason[1]["content"][0]["text"].startswith("🖼️")
    assert paragraphs_after_reason[2]["content"][0]["text"].startswith("🖼️")
    # Each image link node carries the URL as both text and href.
    img_link = paragraphs_after_reason[1]["content"][1]
    assert img_link["text"] == "https://i.imgur.com/a.png"
    assert img_link["marks"][0]["attrs"]["href"] == "https://i.imgur.com/a.png"


def test_build_qa_fail_adf_dedups_images_and_drops_blanks():
    doc = _build_qa_fail_adf(
        "Reason",
        None,
        ["https://i.imgur.com/a.png", "  ", "https://i.imgur.com/a.png", ""],
    )
    assert doc is not None
    image_paragraphs = [
        p for p in doc["content"]
        if p.get("type") == "paragraph"
        and p.get("content") and p["content"][0].get("text", "").startswith("🖼️")
    ]
    assert len(image_paragraphs) == 1
