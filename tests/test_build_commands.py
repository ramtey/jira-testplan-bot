"""Rule Y (real commands and identifiers) and rule T (the ticket base).

From a UAT run on an Android ticket: the plan said ``gradle test`` where the
repo commits a Gradle wrapper and runs unit tests per variant, and the build
check asked adb for the bare ``applicationId`` while the debug build the
tester installs carries an ``applicationIdSuffix``. Fixtures are generic
(``com.acme.*``); nothing here is specific to that ticket.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.app import build_commands as bc
from src.app.build_targets import check_clause
from src.app.code_exploration import Workspace

KTS = """
plugins { id("com.android.application") }
android {
    namespace = "com.acme.forms"
    defaultConfig {
        applicationId = "com.acme.forms"
        versionName = "4.2.0"
        versionCode = 420
    }
    buildTypes {
        getByName("debug") {
            applicationIdSuffix = ".debug"
            isDebuggable = true
        }
        create("staging") {
            initWith(getByName("debug"))
            applicationIdSuffix = ".staging"
        }
        release {
            isMinifyEnabled = true
        }
    }
}
"""

GROOVY_FLAVORS = """
apply plugin: 'com.android.application'
android {
    defaultConfig { applicationId "com.acme.app" }
    flavorDimensions "env"
    productFlavors {
        dev { dimension "env"; applicationIdSuffix ".dev" }
        prod { dimension "env" }
    }
    buildTypes {
        debug { applicationIdSuffix ".debug" }
        release { }
    }
}
"""


def _ids(parsed):
    return {v["name"]: v["application_id"] for v in parsed["variants"]}


def test_each_build_type_gets_its_own_application_id():
    parsed = bc.parse_android_variants("app/build.gradle.kts", KTS)
    assert parsed["module"] == ":app" and parsed["application_id"] == "com.acme.forms"
    assert _ids(parsed) == {"debug": "com.acme.forms.debug", "staging": "com.acme.forms.staging",
                            "release": "com.acme.forms"}
    assert parsed["complete"] is True


def test_flavours_multiply_with_build_types():
    parsed = bc.parse_android_variants("app/build.gradle", GROOVY_FLAVORS)
    assert _ids(parsed) == {"devDebug": "com.acme.app.dev.debug", "prodDebug": "com.acme.app.debug",
                            "devRelease": "com.acme.app.dev", "prodRelease": "com.acme.app"}


def test_several_flavour_dimensions_make_the_list_incomplete():
    text = GROOVY_FLAVORS.replace('flavorDimensions "env"', 'flavorDimensions "env", "tier"')
    assert bc.parse_android_variants("app/build.gradle", text)["complete"] is False


def _evidence():
    tree = ["gradlew", "settings.gradle.kts", "app/build.gradle.kts", "build.gradle.kts"]
    facts = bc.read_command_facts(tree, {"app/build.gradle.kts": KTS, "build.gradle.kts": "plugins {}"}, "android")
    return [{"repo": "acme/forms-android", "commands": facts}]


def test_the_prompt_is_told_the_wrapper_variants_and_test_tasks():
    lines = bc.render_command_facts(_evidence()[0]["commands"])
    text = "\n".join(lines)
    assert "every Gradle command is `./gradlew …`" in text
    assert "debug → com.acme.forms.debug" in text
    assert "`./gradlew :app:testDebugUnitTest`" in text


def _plan(**sections):
    base = {"happy_path": [], "edge_cases": [], "integration_tests": [], "regression_checklist": []}
    return SimpleNamespace(**{**base, **sections})


def test_wrong_commands_and_ids_are_marked_with_what_the_config_has():
    plan = _plan(happy_path=[{
        "title": "t", "steps": [
            "Run `gradle test` in the repo",
            "Run `./gradlew :app:testQaUnitTest`",
            "Run `adb shell pm clear com.acme.forms`",
            "Run `./gradlew :app:testDebugUnitTest`",
            "Run `adb shell dumpsys package com.acme.forms.debug`",
        ], "expected": "ok"}],
        regression_checklist=["🔴 Reinstall — Steps: 1. `adb uninstall com.acme.forms` Expected: gone. Check: adb"])
    findings = bc.check_commands(plan, _evidence())
    steps = plan.happy_path[0]["steps"]
    assert steps[0].startswith(bc.COMMAND_MARKER) and "use `./gradlew`" in steps[0]
    assert "variant 'Qa', which the build config does not define" in steps[1]
    assert "`com.acme.forms` is not a variant's id" not in steps[2]  # release id is a real variant
    assert not steps[3].startswith(bc.COMMAND_MARKER) and not steps[4].startswith(bc.COMMAND_MARKER)
    assert plan.regression_checklist[0].startswith("🔴 ") and len(findings) == 2


def test_a_base_id_with_no_unsuffixed_variant_is_flagged():
    text = KTS.replace("release {\n            isMinifyEnabled = true", 'release {\n            applicationIdSuffix = ".rel"')
    facts = bc.read_command_facts(["app/build.gradle.kts"], {"app/build.gradle.kts": text}, "android")
    plan = _plan(happy_path=[{"title": "t", "steps": ["adb shell pm clear com.acme.forms"], "expected": "x"}])
    bc.check_commands(plan, [{"commands": facts}])
    assert "application id `com.acme.forms` is not a variant's id" in plan.happy_path[0]["steps"][0]


def test_unknown_is_never_wrong():
    """An unread build file, or variants the parser cannot list, flag nothing."""
    facts = bc.read_command_facts(["app/build.gradle.kts"], {}, "android")
    assert facts["unread"] == ["app/build.gradle.kts"] and facts["android_apps"] == []
    plan = _plan(happy_path=[{"title": "t", "steps": ["adb shell pm clear com.acme.forms.qa",
                                                      "./gradlew :app:testQaUnitTest"], "expected": "x"}])
    assert bc.check_commands(plan, [{"commands": facts}]) == []


def test_a_script_package_json_does_not_define_is_flagged():
    facts = bc.read_command_facts(["package.json", "pnpm-lock.yaml"],
                                  {"package.json": '{"scripts": {"test": "vitest", "e2e": "playwright test"}}'},
                                  "web")
    assert facts["package_manager"] == "pnpm"
    plan = _plan(happy_path=[{"title": "t", "steps": ["pnpm run test:unit", "pnpm run e2e"], "expected": "x"}])
    bc.check_commands(plan, [{"commands": facts}])
    assert "has no script 'test:unit'" in plan.happy_path[0]["steps"][0]
    assert not plan.happy_path[0]["steps"][1].startswith(bc.COMMAND_MARKER)


def test_the_android_build_check_names_each_variants_id():
    t = {"repo": "acme/forms-android", "commit": "a" * 40, "platform": "android",
         "app": {"version": "4.2.0", "build": "420", "bundle_ids": ["com.acme.forms"], "file": "app/build.gradle.kts"},
         "commands": _evidence()[0]["commands"], "gate": None, "deploy": []}
    clause = check_clause(t)
    assert "com.acme.forms.debug (debug)" in clause and "com.acme.forms.staging (staging)" in clause
    assert "dumpsys package com.acme.forms |" not in clause


# --- rule T ------------------------------------------------------------------

class _GH:
    def __init__(self):
        self.calls = []

    async def compare_commits(self, repo, base, head):
        self.calls.append((repo, base, head))
        return {"status": "ahead", "merge_base": "b" * 40}

    async def fetch_files_at_ref(self, repo, ref, paths, **_):
        return {p: f"old code at {ref[:7]}\nline two" for p in paths}


@pytest.mark.asyncio
async def test_the_ticket_base_is_the_merge_base_of_the_first_pr():
    gh = _GH()
    prs = [{"number": 12, "base_sha": "c" * 40, "head_sha": "d" * 40},
           {"number": 9, "base_sha": "e" * 40, "head_sha": "f" * 40},
           {"number": 3, "head_sha": "9" * 40}]  # no base known: skipped
    got = await bc.ticket_base(gh, "acme/forms-android", prs)
    assert got == {"sha": "b" * 40, "pr": 9}
    assert gh.calls == [("acme/forms-android", "e" * 40, "f" * 40)]


@pytest.mark.asyncio
async def test_no_answer_means_no_base_never_a_guess():
    class _Silent(_GH):
        async def compare_commits(self, *a):
            return None
    assert await bc.ticket_base(_Silent(), "acme/x", [{"number": 1, "base_sha": "a", "head_sha": "b"}]) is None


@pytest.mark.asyncio
async def test_the_workspace_reads_a_file_at_the_ticket_base():
    prs = [{"repository": "acme/forms-android", "head_sha": "h" * 40, "files_changed": [{"filename": "a.kt"}]}]
    ws = Workspace(_GH(), prs, base_refs={"acme/forms-android": "b" * 40})
    out = await ws.read_file("acme/forms-android", "a.kt", at="base")
    assert "ticket base bbbbbbb" in out and "old code at bbbbbbb" in out
    assert ("acme/forms-android", "a.kt") in ws.read_files
    none = await Workspace(_GH(), prs).read_file("acme/forms-android", "a.kt", at="base")
    assert none.startswith("No ticket base is known")
