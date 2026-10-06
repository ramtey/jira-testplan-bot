"""Product risk profiles: matching, the prompt block, tags, and holdout.

The properties that matter:

* A profile reaches only its product's tickets — a repo match or a summary
  match, with DigiSign-only summaries excluded unless a PR changed a Forms
  repo.
* A case tagged from a profile survives quarantine only when the tag names
  an id the profile really defines. SK-2620's escaped bugs were all outside
  the diff, which is exactly what quarantine removes.
* The eval holdout removes every line citing a held-out ticket, including
  a bullet's continuation lines, so a replay can't read its own answer.
"""

from types import SimpleNamespace

from src.app import risk_profiles
from src.app.risk_profiles import (
    holdout,
    load_profiles,
    match_profiles,
    match_profiles_for_batch,
    parse_profile,
    profile_tag_is_known,
    render_risk_profile_guidance,
)
from src.app.services.test_plan_generator import quarantine_ungrounded_cases

PROFILE = """---
name: Forms
match:
  repos: ["files-api", "forms-"]
  summary: ['\\[Forms', 'share file']
  exclude_summary: ['^\\s*\\[(DS3|DIGI)\\]']
---
## 2. User types

| Standalone user | can share with anyone | SK-2620 |
| Brokerage agent | restricted | SK-2665 |

### T1. Identity

**T1a. Who-has-access code** — signal: grants.
- Remind & Resend still works as the owner (SK-2620; AP-2071)
- Revoke really removes access, checked with
  a clean account (AP-2871; SK-2630)
- History events in Forms and DS3 (Nick)
"""


def write_profile(tmp_path, text=PROFILE, name="forms.md"):
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def dev(*repos):
    return {"pull_requests": [{"repository": r} for r in repos]}


def test_parses_ids_and_patterns(tmp_path):
    [p] = load_profiles(write_profile(tmp_path))
    assert p.name == "Forms"
    assert {"§2", "T1", "T1a"} <= p.ids


def test_matches_by_repo_even_when_summary_is_excluded(tmp_path):
    profiles = load_profiles(write_profile(tmp_path))
    assert match_profiles(summary="[DS3] token change", development_info=dev("skyslope/files-api"), profiles=profiles)


def test_matches_by_summary_and_excludes_digisign_only(tmp_path):
    profiles = load_profiles(write_profile(tmp_path))
    assert match_profiles(summary="[Forms] envelope status", development_info=None, profiles=profiles)
    assert not match_profiles(summary="[DS3] [Forms] signer page", development_info=None, profiles=profiles)
    assert not match_profiles(summary="Agent Calc: HOA", development_info=dev("acme/agent-calculator"), profiles=profiles)


def test_batch_matches_when_any_ticket_does(tmp_path):
    profiles = load_profiles(write_profile(tmp_path))
    tickets = [{"summary": "Agent Calc"}, {"summary": "UX DEL : Share File : Touchpoints"}]
    assert [p.name for p in match_profiles_for_batch(tickets, profiles)] == ["Forms"]


def test_unusable_files_are_skipped(tmp_path):
    write_profile(tmp_path, "no front matter", "a.md")
    write_profile(tmp_path, "---\nname: X\nmatch: {}\n---\nbody", "b.md")
    write_profile(tmp_path, "---\nname: Y\nmatch:\n  summary: ['(']\n---\nbody", "c.md")
    assert load_profiles(tmp_path) == []
    assert load_profiles(None) == []
    assert load_profiles(tmp_path / "missing") == []


def test_render_is_empty_without_a_match_and_carries_body_with_one(tmp_path):
    assert render_risk_profile_guidance([]) == ""
    block = render_risk_profile_guidance(load_profiles(write_profile(tmp_path)))
    assert "Remind & Resend" in block
    assert "[<profile name> risk profile · <id>]" in block
    assert "ONLY test" in block  # names the rule it overrides


def test_tag_must_name_a_defined_id(tmp_path):
    profiles = load_profiles(write_profile(tmp_path))
    assert profile_tag_is_known("[Forms risk profile · T1a] Remind & Resend as owner", profiles)
    assert profile_tag_is_known("[forms risk profile · §2] Standalone user shares", profiles)
    assert not profile_tag_is_known("[Forms risk profile · T9] invented", profiles)
    assert not profile_tag_is_known("[DigiSign risk profile · T1a] wrong profile", profiles)
    assert not profile_tag_is_known("Remind & Resend as owner", profiles)


def test_quarantine_keeps_profile_tagged_cases_only(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_profiles, "configured_profiles", lambda: load_profiles(write_profile(tmp_path)))
    ungrounded = {"needs_manual_verification": True, "expected_verified": False}
    tagged = {"title": "[Forms risk profile · T1a] Remind & Resend still sends as the owner", **ungrounded}
    invented = {"title": "[Forms risk profile · T42] Something", **ungrounded}
    plain = {"title": "Guessing at a control", **ungrounded}
    plan = SimpleNamespace(happy_path=[], edge_cases=[tagged, invented, plain], integration_tests=[])
    moved = quarantine_ungrounded_cases(plan)
    assert plan.edge_cases == [tagged]
    assert moved == [invented, plain]


def test_holdout_drops_cited_lines_and_their_continuations(tmp_path):
    [p] = load_profiles(write_profile(tmp_path))
    h = holdout(p, {"SK-2620", "SK-2630"})
    assert "Remind & Resend" not in h.body
    assert "Standalone user" not in h.body
    assert "Revoke really removes access" not in h.body
    assert "a clean account" not in h.body
    assert "Brokerage agent" in h.body
    assert "History events" in h.body
    assert "T1a" in h.ids
    assert holdout(p, set()) is p


def test_parse_profile_uses_file_stem_when_unnamed(tmp_path):
    p = parse_profile("---\nmatch:\n  repos: [x]\n---\nbody", tmp_path / "digisign.md")
    assert p.name == "digisign"


def test_holdout_drops_a_list_introduced_by_a_cited_paragraph(tmp_path):
    # Nick's Phase II bullets didn't repeat SK-2620, only their intro did,
    # and "Contacts and MLS import" leaked into the first Forms eval.
    text = PROFILE + """
**Surfaces a newly-granted person reaches** — check the ones the change
could affect (Nick's SK-2620 Phase II checklist):
- Contacts and MLS import inside the file
- Envelope history

**Unrelated note:**
- Kept bullet
"""
    [p] = load_profiles(write_profile(tmp_path, text))
    h = holdout(p, {"SK-2620"})
    assert "Surfaces a newly-granted" not in h.body
    assert "Contacts and MLS import" not in h.body
    assert "Envelope history" not in h.body
    assert "Unrelated note" in h.body and "Kept bullet" in h.body
