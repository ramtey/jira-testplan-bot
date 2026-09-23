"""
Tests for `_uploads_in_request_order` — the guard that keeps a QA pass
comment's screenshots in the order the tester attached them.

Jira's multipart attachment endpoint echoes back one object per file but
promises nothing about their order, and the ADF builder renders images in
exactly the order it receives them.
"""

from src.app.jira_client import _uploads_in_request_order


def _files(*names: str) -> list[tuple[str, bytes, str]]:
    return [(name, b"x", "image/png") for name in names]


def _uploaded(*pairs: tuple[str, str]) -> list[dict]:
    return [{"id": att_id, "filename": name} for att_id, name in pairs]


def test_shuffled_response_is_restored_to_attach_order():
    uploaded = _uploaded(("3", "c.png"), ("1", "a.png"), ("2", "b.png"))
    ordered = _uploads_in_request_order(uploaded, _files("a.png", "b.png", "c.png"))
    assert [e["filename"] for e in ordered] == ["a.png", "b.png", "c.png"]


def test_duplicate_filenames_keep_one_slot_each():
    uploaded = _uploaded(("2", "shot.png"), ("1", "shot.png"), ("3", "later.png"))
    ordered = _uploads_in_request_order(
        uploaded, _files("shot.png", "shot.png", "later.png")
    )
    assert [e["id"] for e in ordered] == ["2", "1", "3"]


def test_renamed_attachment_lands_last_instead_of_disappearing():
    uploaded = _uploaded(("1", "a.png"), ("2", "b_1.png"))
    ordered = _uploads_in_request_order(uploaded, _files("b.png", "a.png"))
    assert [e["filename"] for e in ordered] == ["a.png", "b_1.png"]


def test_unexpected_shapes_are_dropped_not_raised():
    assert _uploads_in_request_order({"errors": []}, _files("a.png")) == []
    ordered = _uploads_in_request_order(
        ["junk", {"id": "1", "filename": "a.png"}], _files("a.png")
    )
    assert ordered == [{"id": "1", "filename": "a.png"}]
