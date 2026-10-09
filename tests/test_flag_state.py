"""What the flags a ticket touches serve, per environment.

Why this file exists
--------------------
SK-2687's plan told testers to toggle `inPersonSigning` per user — a
LaunchDarkly write QA may not make — and never noticed that integ serves it to
everyone, so its flag-off case could not run there. A hand-run plan read the
flag and got both right.

Pinned here: which keys count as "touched" (camelCase React reads of kebab
keys, C# string literals, no bare common words), the one-line summary a tester
acts on, and that a failed read is "could not check", never "off".
"""

from __future__ import annotations

import pytest

from src.app.flag_state import (
    camel,
    find_flag_keys,
    read_flag_state,
    render_flag_state,
    summarize_env,
)

# Shape as LaunchDarkly returned it for digisign3/inPersonSigning on 2026-10-09.
IN_PERSON = {
    "key": "inPersonSigning", "name": "In Person Signing",
    "variations": [{"value": True}, {"value": False}],
    "environments": {
        "staging": {"on": True, "offVariation": 1, "targets": [], "contextTargets": [],
                    "rules": [{"clauses": [{"attribute": "subscriberId", "op": "in",
                                            "values": [3356, "3356"]}], "variation": 0}],
                    "fallthrough": {"variation": 1}, "prerequisites": []},
        "integ": {"on": True, "offVariation": 1, "rules": [], "fallthrough": {"variation": 0}},
        "dev": {"on": False, "offVariation": 1},
    },
}


def test_camel():
    assert camel("is-file-sharing-enabled") == "isFileSharingEnabled"
    assert camel("inPersonSigning") == "inPersonSigning"


def test_find_keys_camel_kebab_literal_and_order():
    keys = ["is-file-sharing-enabled", "inPersonSigning", "sign-now", "debug", "unused-flag"]
    texts = [
        "const ok = canSignInPerson(flags) && flags?.inPersonSigning;",
        'const { isFileSharingEnabled } = useFlags();',
        '_ldClient.BoolVariation("sign-now", user, false);',
        "if (debug) log()",  # a bare common word is not a flag reference
    ]
    assert find_flag_keys(texts, keys) == ["inPersonSigning", "is-file-sharing-enabled", "sign-now"]


def test_single_word_key_counts_only_in_quotes():
    assert find_flag_keys(['variation("debug", false)'], ["debug"]) == ["debug"]
    assert find_flag_keys(["debug mode"], ["debug"]) == []


def test_partial_identifier_is_not_a_match():
    assert find_flag_keys(["useSignNowFlow()"], ["sign-now"]) == []


def test_summaries_a_tester_can_act_on():
    envs = IN_PERSON["environments"]
    assert summarize_env(IN_PERSON, envs["staging"]) == \
        "ON: subscriberId in [3356] -> true; everyone else -> false"
    assert summarize_env(IN_PERSON, envs["integ"]) == "ON: everyone -> true"
    assert summarize_env(IN_PERSON, envs["dev"]) == "OFF: everyone gets false"


def test_rollout_and_targets():
    env = {"on": True, "targets": [{"values": ["a", "b"], "variation": 0}],
           "rules": [], "fallthrough": {"rollout": {"variations": [
               {"variation": 0, "weight": 25000}, {"variation": 1, "weight": 75000}]}}}
    assert summarize_env(IN_PERSON, env) == \
        "ON: 2 individually targeted -> true; everyone else -> 25% true / 75% false"


class FakeLD:
    def __init__(self, keys, flags):
        self.keys, self.flags, self.asked = keys, flags, []

    async def flag_keys(self, project):
        return self.keys

    async def flag(self, project, key, env_keys):
        self.asked.append((key, tuple(env_keys)))
        return self.flags.get(key)


SOURCES = {"acme/web": {"project": "digisign3",
                        "envs": {"integ": "integ", "staging": "staging"}}}


@pytest.mark.asyncio
async def test_reads_only_touched_flags_in_configured_envs():
    ld = FakeLD(["inPersonSigning", "other-flag"], {"inPersonSigning": IN_PERSON})
    state = await read_flag_state(ld, {"acme/web": ["flags?.inPersonSigning"]}, SOURCES)
    assert ld.asked == [("inPersonSigning", ("integ", "staging"))]
    text = render_flag_state(state)
    assert "integ: ON: everyone -> true" in text
    assert "staging: ON: subscriberId in [3356] -> true; everyone else -> false" in text


@pytest.mark.asyncio
async def test_unreadable_is_could_not_check_never_off():
    ld = FakeLD(["inPersonSigning"], {})
    state = await read_flag_state(ld, {"acme/web": ["flags?.inPersonSigning"]}, SOURCES)
    text = render_flag_state(state)
    assert "COULD NOT CHECK" in text and "OFF" not in text

    class Down(FakeLD):
        async def flag_keys(self, project):
            return None
    state = await read_flag_state(Down([], {}), {"acme/web": ["x"]}, SOURCES)
    assert state[0]["error"] and "COULD NOT CHECK" in render_flag_state(state)


@pytest.mark.asyncio
async def test_unmapped_repo_reads_nothing():
    ld = FakeLD(["inPersonSigning"], {"inPersonSigning": IN_PERSON})
    assert await read_flag_state(ld, {"acme/other": ["inPersonSigning"]}, SOURCES) == []
    assert ld.asked == []


@pytest.mark.asyncio
async def test_prompt_carries_flag_state_and_the_no_write_rule():
    from tests.test_bounce_in_prompts import _PromptOnlyClient
    ld = FakeLD(["inPersonSigning"], {"inPersonSigning": IN_PERSON})
    state = await read_flag_state(ld, {"acme/web": ["flags?.inPersonSigning"]}, SOURCES)
    dev = {"pull_requests": [], "commits": [], "branches": [], "flag_state": state}
    prompt = _PromptOnlyClient()._build_prompt("SK-1", "Owner only", "body", {}, dev)
    assert "staging: ON: subscriberId in [3356] -> true" in prompt
    assert "NEVER tell a tester to change a flag" in prompt


def test_only_internal_addresses_reach_the_plan(monkeypatch):
    from src.app.config import settings
    monkeypatch.setattr(settings, "ld_internal_email_domains", ["skyslope.com"])
    env = {"on": True, "rules": [{"clauses": [{"attribute": "email", "op": "in", "values": [
        "123DevBroker@skyslope.com", "someone@freedrops.org", "client@gmail.com"]}],
        "variation": 0}], "fallthrough": {"variation": 1}}
    line = summarize_env(IN_PERSON, env)
    assert "123DevBroker@skyslope.com" in line
    assert "freedrops" not in line and "gmail" not in line
    assert "2 other address(es)" in line


def test_with_no_internal_domain_every_address_is_hidden(monkeypatch):
    from src.app.config import settings
    monkeypatch.setattr(settings, "ld_internal_email_domains", [])
    env = {"on": True, "rules": [{"clauses": [{"attribute": "email", "op": "in",
        "values": ["a@skyslope.com"]}], "variation": 0}], "fallthrough": {"variation": 1}}
    assert "a@skyslope.com" not in summarize_env(IN_PERSON, env)
