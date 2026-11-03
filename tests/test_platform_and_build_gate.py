"""Rules N (platform capabilities) and O (build gate).

N: a step must be possible on the app as shipped. The fixture below has the
shape of a real iOS project (sanitized): iPhone + iPad, portrait-only on
iPhone, every orientation on iPad, a push entitlement. "Rotate to landscape"
is therefore not applicable on iPhone and valid on iPad.

O: the build gate is the PR whose branch, title or commits name the ticket
key; a later PR for the same ticket is "build must be at or after", and a PR
that only got linked is never presented as the ticket's change.
"""

from __future__ import annotations

import plistlib
from types import SimpleNamespace

import pytest

from src.app import build_targets as bt
from src.app import evidence_checks as ev
from src.app import platform_capabilities as pc

PBX = "Acme.xcodeproj/project.pbxproj"
PLIST = "Acme/Application/Info.plist"
ENT = "Acme/Acme.entitlements"


def _block(**kv) -> str:
    body = "".join(f"\t\t\t\t{k} = {v};\n" for k, v in kv.items())
    return f"\t\tA1 /* Debug */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{body}\t\t\t}};\n"


APP = dict(PRODUCT_BUNDLE_IDENTIFIER='"com.acme.mobile"', TARGETED_DEVICE_FAMILY='"1,2"',
           INFOPLIST_FILE=PLIST, CODE_SIGN_ENTITLEMENTS=ENT)
PBXPROJ = (_block(SDKROOT="iphoneos", INFOPLIST_FILE="Acme/Info.plist")                # project level
           + _block(**APP)
           + _block(**{**APP, "PRODUCT_BUNDLE_IDENTIFIER": '"com.acme.mobile.integ"'})  # env config
           + _block(PRODUCT_BUNDLE_IDENTIFIER='"com.acme.mobileTests"', TARGETED_DEVICE_FAMILY="1",
                    TEST_HOST='"$(BUILT_PRODUCTS_DIR)/Acme.app/Acme"')
           + _block(PRODUCT_BUNDLE_IDENTIFIER='"com.acme.mobile.share"', TARGETED_DEVICE_FAMILY="1",
                    SKIP_INSTALL="YES"))


def _plist(d: dict) -> str:
    return plistlib.dumps(d).decode()


INFO = _plist({
    "UISupportedInterfaceOrientations": ["UIInterfaceOrientationPortrait"],
    "UISupportedInterfaceOrientations~ipad": ["UIInterfaceOrientationPortrait",
                                              "UIInterfaceOrientationPortraitUpsideDown",
                                              "UIInterfaceOrientationLandscapeLeft",
                                              "UIInterfaceOrientationLandscapeRight"],
    "UISupportedInterfaceOrientations~iphone": ["UIInterfaceOrientationPortrait"],
})
ENTS = _plist({"aps-environment": "development",
               "com.apple.security.application-groups": ["group.com.acme"]})


def _caps(**over):
    files = {PLIST: INFO, ENT: ENTS, **over.pop("files", {})}
    return pc.parse_ios_capabilities(PBX, over.pop("pbxproj", PBXPROJ), files)


def _plan(*cases, lines=()):
    return SimpleNamespace(happy_path=list(cases), edge_cases=[], integration_tests=[],
                           security_negative_tests=[], regression_checklist=list(lines))


def _case(title, steps, **kw):
    return {"title": title, "steps": list(steps), "expected": "It works.", "surface": "mobile", **kw}


def _targets(cap):
    return [{"repo": "acme/ios-client", "platform": "ios", "capabilities": cap}]


# --- N: reading the project -------------------------------------------------

def test_reads_the_app_target_not_tests_extensions_or_project_level():
    cap = _caps()
    assert cap["devices"] == ["iPhone", "iPad"]
    assert 'TARGETED_DEVICE_FAMILY = "1,2"' in cap["devices_source"]
    assert cap["orientations"]["iPhone"] == ["portrait"]
    assert "landscape left" in cap["orientations"]["iPad"]
    assert cap["orientations_source"]["iPhone"].endswith("UISupportedInterfaceOrientations~iphone")
    assert cap["entitlements"] == ["aps-environment", "com.apple.security.application-groups"]


def test_generated_plist_build_settings_are_read_when_there_is_no_plist_key():
    pbx = _block(PRODUCT_BUNDLE_IDENTIFIER='"com.acme.mobile"', TARGETED_DEVICE_FAMILY="1",
                 GENERATE_INFOPLIST_FILE="YES",
                 INFOPLIST_KEY_UISupportedInterfaceOrientations_iPhone="UIInterfaceOrientationPortrait")
    cap = pc.parse_ios_capabilities(PBX, pbx, {})
    assert cap["devices"] == ["iPhone"]
    assert cap["orientations"] == {"iPhone": ["portrait"]}


def test_facts_name_their_files():
    lines = pc.render_capabilities(_caps())
    assert any("On iPhone the app supports portrait only" in l and "~iphone" in l for l in lines)
    assert any("On iPad the app supports every orientation" in l for l in lines)
    entry = {"repo": "acme/ios-client", "ref": "abc1234def", "error": None, "test_sources": {},
             "truncated": [], "build": {"findings": [], "suites": {}}, "platform": "ios",
             "app_version": None, "capabilities": _caps()}
    assert "PLATFORM FACT (rule N): On iPhone the app supports portrait only" in ev.render_repo_evidence([entry])


# --- N: marking -------------------------------------------------------------

def test_landscape_on_iphone_is_not_applicable_and_keeps_the_step_number():
    case = _case("Signature pad", ["Open a document", "Rotate the iPhone to landscape", "Sign"])
    out = pc.flag_unsupported_steps(_plan(case), _targets(_caps()))
    assert [o["where"] for o in out] == ["step 2"]
    assert len(case["steps"]) == 3
    assert case["steps"][1].startswith("Not applicable: iPhone is portrait-only")
    assert "~iphone" in case["steps"][1]


def test_landscape_with_no_device_named_runs_only_on_ipad():
    case = _case("Signature pad", ["Rotate the device to landscape and confirm the pad resizes"])
    pc.flag_unsupported_steps(_plan(case), _targets(_caps()))
    assert case["steps"][0].startswith("Only on iPad:")


def test_landscape_in_an_ipad_case_is_left_alone():
    case = _case("Signature pad on iPad", ["Rotate to landscape", "Sign"])
    assert pc.flag_unsupported_steps(_plan(case), _targets(_caps())) == []
    assert case["steps"][0] == "Rotate to landscape"


def test_a_case_about_a_device_the_app_does_not_target_is_marked_not_applicable():
    pbx = PBXPROJ.replace('TARGETED_DEVICE_FAMILY = "1,2"', "TARGETED_DEVICE_FAMILY = 1")
    case = _case("Split view layout on iPad", ["Open the app on an iPad"])
    out = pc.flag_unsupported_steps(_plan(case), _targets(_caps(pbxproj=pbx)))
    assert out[0]["where"] == "case" and out[0]["kind"] == "na"
    assert case["expected"].startswith("Not applicable: the app does not target iPad")
    assert case["not_applicable"]["rule"] == "N"


def test_an_entitlement_the_app_lacks_is_not_applicable_and_one_it_has_is_fine():
    a = _case("Login", ["Tap Sign in with Apple"])
    b = _case("Alerts", ["Send a push notification to the device"])
    pc.flag_unsupported_steps(_plan(a, b), _targets(_caps()))
    assert a["steps"][0].startswith("Not applicable: the app has no com.apple.developer.applesignin")
    assert b["steps"][0] == "Send a push notification to the device"


def test_unknown_is_not_unsupported():
    # Orientations set in the app delegate: the plist decides nothing.
    over = {"Acme/AppDelegate.swift": "func application(_ a: UIApplication, "
                                      "supportedInterfaceOrientationsFor w: UIWindow?) -> UIInterfaceOrientationMask"}
    case = _case("Pad", ["Rotate the iPhone to landscape"])
    assert pc.flag_unsupported_steps(_plan(case), _targets(_caps(files=over))) == []
    # Info.plist and entitlements not read at all.
    cap = pc.parse_ios_capabilities(PBX, PBXPROJ, {})
    assert PLIST in cap["unread"] and cap["entitlements"] is None
    case = _case("Pad", ["Rotate the iPhone to landscape", "Tap Sign in with Apple"])
    assert pc.flag_unsupported_steps(_plan(case), _targets(cap)) == []


def test_text_that_already_says_not_applicable_is_not_marked_twice_and_regression_lines_count():
    case = _case("Pad", ["Not applicable: landscape on iPhone — portrait-only"])
    plan = _plan(case, lines=["Rotate the iPhone to landscape on the document list"])
    out = pc.flag_unsupported_steps(plan, _targets(_caps()))
    assert [o["case_id"] for o in out] == ["regression_checklist:0"]
    assert len(plan.regression_checklist) == 1


def test_web_cases_and_non_ios_tickets_are_not_touched():
    case = _case("Pad", ["Rotate the iPhone to landscape"], surface="web_ui")
    assert pc.flag_unsupported_steps(_plan(case), _targets(_caps())) == []
    assert pc.flag_unsupported_steps(_plan(_case("x", ["landscape"])), [{"platform": "web"}]) == []


# --- O: build gate ----------------------------------------------------------

def _pr(n, sha, merged_at, **kw):
    return {"repository": "acme/mobile-api", "number": n, "url": f"https://github.com/acme/mobile-api/pull/{n}",
            "status": "MERGED", "merge_commit_sha": sha, "merged_at": merged_at,
            "files_changed": [{"filename": "src/api.ts"}], **kw}


def test_key_match_is_exact_and_reads_branch_title_then_commits():
    assert bt.names_key("feature/sk-12-login", "SK-12")
    assert not bt.names_key("SK-123 other", "SK-12")
    assert bt.match_ticket({"source_branch": "x", "title": "Fix SK-12"}, ["SK-12"]) == "title"
    assert bt.match_ticket({"title": "chore"}, ["SK-12"], ["SK-12: wire it"]) == "commit"
    assert bt.match_ticket({"title": "chore"}, ["SK-12"], ["unrelated"]) == "none"
    assert bt.match_ticket({"title": "chore"}, ["SK-12"], None) == "unread"


def test_an_unrelated_linked_pr_is_never_the_ticket_change():
    mine = _pr(10, "aaaaaaa1", "2026-10-01", ticket_match="branch", ticket_keys=["SK-12"])
    other = _pr(11, "bbbbbbb2", "2026-10-05", ticket_match="none", ticket_keys=["SK-12"])
    [t] = bt.build_targets([mine, other])
    assert t["commit"] == "aaaaaaa1"  # was bbbbbbb2: the newest merge won whatever it was
    s = bt.gate_sentence(t)
    assert "This ticket's change is PR #10 (merge commit aaaaaaa)." in s
    assert "PR #11 is also linked to SK-12" in s and "not this ticket's build gate" in s


def test_a_later_pr_for_the_ticket_is_at_or_after_not_the_change():
    first = _pr(10, "aaaaaaa1", "2026-10-01", ticket_match="title", ticket_keys=["SK-12"])
    fix = _pr(14, "ccccccc3", "2026-10-09", ticket_match="commit", ticket_keys=["SK-12"])
    [t] = bt.build_targets([fix, first])
    assert t["commit"] == "ccccccc3"
    s = bt.check_clause(t)
    assert "This ticket's change is PR #10 (merge commit aaaaaaa)." in s
    assert "Build must be at or after ccccccc (PR #14" in s
    assert "contains merge commit ccccccc" in s


def test_no_pr_naming_the_ticket_is_an_open_question_not_a_fact():
    a = _pr(10, "aaaaaaa1", "2026-10-01", ticket_match="none", ticket_keys=["SK-12"])
    [t] = bt.build_targets([a])
    assert t["commit"] == "aaaaaaa1" and t["gate"]["verified"] is False
    assert bt.gate_sentence(t).startswith("Open question: PR #10 (merge commit aaaaaaa)")
    b = _pr(10, "aaaaaaa1", "2026-10-01", ticket_match="unread", ticket_keys=["SK-12"])
    assert "its commits could not be read" in bt.gate_sentence(bt.build_targets([b])[0])


def test_unannotated_prs_gate_as_before():
    a, b = _pr(10, "aaaaaaa1", "2026-10-01"), _pr(11, "bbbbbbb2", "2026-10-05")
    [t] = bt.build_targets([a, b])
    assert t["commit"] == "bbbbbbb2" and t["gate"]["verified"] is True


@pytest.mark.asyncio
async def test_annotate_reads_commits_only_when_branch_and_title_do_not_name_the_key():
    calls = []

    class GH:
        async def fetch_pr_commit_messages(self, url):
            calls.append(url)
            return None if url.endswith("/12") else ["SK-12 follow-up"]

    prs = [_pr(10, "a", "1", source_branch="SK-12-x"), _pr(11, "b", "2"), _pr(12, "c", "3"),
           _pr(13, "d", "4", borrowed_from="SK-9", title="SK-9 part")]
    await bt.annotate_ticket_match(GH(), prs, ["SK-12"])
    assert [p["ticket_match"] for p in prs] == ["branch", "commit", "unread", "title"]
    assert prs[3]["ticket_keys"] == ["SK-9"]
    assert len(calls) == 2
