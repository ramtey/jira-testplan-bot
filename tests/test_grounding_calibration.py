"""Calibration of the code re-read: correct claims must stay verified.

Why this file exists
--------------------
On 2026-10-23 four SK-2687 runs marked 21-45% of their expected results
unverified, up from about 21%. The logic-claims check had downgraded correct
cases for three reasons, each a false failure:

1. "restates the rule … without userInfo is set" — the case said "not a
   delegate", which reads ``userInfo.isDelegate`` and cannot hold unless
   ``userInfo`` is set. The predicate atom had dropped the object it is read
   from, so the null check looked separately required.
2. "its logic_claims rule names too little of … (isEnvelopeOwner) to check" —
   a rule cannot name two conditions of a function that has fewer than two.
3. "adds 'Sign In Person', which the cited code does not contain" — the label
   is rendered from a JSX expression (``{busy ? 'Starting…' : 'Sign In
   Person'}``), which the UI-string reader did not look inside.

The guard against real omissions stays: a restatement that skips a business
condition is still downgraded (see test_grounding_verification.py).
"""

from __future__ import annotations

import pytest

from src.app import grounding_verification as gv
from src.app import source_facts as sf
from tests.test_grounding_verification import FakeGitHub

CAN_SIGN = """export function canSignInPerson(flags, userInfo, isStartSafe) {
  return Boolean(flags?.inPersonSigning) && userInfo != null && !userInfo.isDelegate && !isStartSafe;
}"""

SUMMARY = """export function Row({ signer, startingSignerId, go }) {
  return (
    <Link className={classes.signInPerson} onClick={go}>
      {startingSignerId === signer.signerId ? 'Starting\\u2026' : 'Sign In Person'}
    </Link>
    <Box>{t('history.signers')}</Box>
  );
}"""

IS_OWNER = """export function isEnvelopeOwner(userInfo, envelopeSenderId) {
  const caller = userInfo?.senderId?.trim();
  const owner = envelopeSenderId?.trim();
  return caller === owner;
}"""


def _atoms(code: str):
    lines = code.splitlines()
    return sf.condition_atoms("x.ts", [(i + 1, line) for i, line in enumerate(lines)][1:])


# ── 1. a property named implies its object's null check ──────────────────────

def test_naming_a_property_implies_the_null_check_on_its_object():
    atoms = _atoms(CAN_SIGN)
    named = {a.display for a in sf.atoms_mentioned(atoms, "not a delegate and not a StartSafe account")}
    assert "userInfo is set" in named


def test_a_real_omission_is_still_an_omission():
    atoms = _atoms(CAN_SIGN)
    named = {a.display for a in sf.atoms_mentioned(atoms, "not a StartSafe account")}
    assert "userInfo is set" not in named and "isDelegate" not in named


def test_the_predicate_keeps_the_object_it_is_read_from():
    is_delegate = next(a for a in _atoms(CAN_SIGN) if a.display == "isDelegate")
    assert is_delegate.receiver == ("userInfo",)


# ── 3. labels rendered from a JSX expression are UI text ──────────────────────

def test_jsx_expression_literals_are_ui_strings():
    texts = [u.text for u in sf.ui_strings("Summary.tsx", SUMMARY)]
    assert "Sign In Person" in texts
    assert "Starting…" in texts            # … decoded, not "Startingu2026"
    assert "history.signers" not in texts  # an i18n key, not shown text


def test_class_names_and_keys_in_braces_stay_out():
    tsx = "<div className={'row-active'}>{'id'}</div>"
    assert sf.ui_strings("x.tsx", tsx) == []


# ── 2 and end-to-end: a correct claim on SK-2687-shaped code stays verified ──

REPO = "acme/web"
PRS = [{
    "repository": REPO, "number": 206, "status": "MERGED", "head_sha": "abc1234",
    "files_changed": [
        {"filename": "src/canSign.ts", "status": "modified", "patch": "@@ +1 @@\n+x"},
        {"filename": "src/isOwner.ts", "status": "modified", "patch": "@@ +1 @@\n+x"},
        {"filename": "src/Summary.tsx", "status": "modified", "patch": "@@ +1 @@\n+x"},
    ],
}]


def _gh():
    files = {"src/canSign.ts": CAN_SIGN, "src/isOwner.ts": IS_OWNER, "src/Summary.tsx": SUMMARY}
    return FakeGitHub({REPO: list(files)}, {REPO: files})


@pytest.mark.asyncio
async def test_a_single_condition_helper_cannot_be_named_too_little():
    index = await sf.read_source_index(_gh(), PRS)
    names, problems = gv._verify_logic_claim(
        {"rule": "the caller's senderId equals the envelope's sender id", "source": "src/isOwner.ts:1-5",
         "function": "isEnvelopeOwner"}, index, set())
    assert problems == [] and names == {"isEnvelopeOwner"}


@pytest.mark.asyncio
async def test_a_correct_expected_result_with_a_label_from_another_file_stays_verified():
    index = await sf.read_source_index(_gh(), PRS)
    case = {
        "title": "Forms delegate sees no Sign In Person on history",
        "steps": ["As the TC, open the owner's envelope history", "Look for 'Sign In Person'"],
        "expected": "No 'Sign In Person' link: the caller is not a delegate check fails, "
                    "so canSignInPerson is false for this caller.",
        "expected_verified": True,
        "expected_source": "src/canSign.ts:1-3",
        "logic_claims": [{"rule": "flag inPersonSigning on, not a delegate, not a StartSafe account",
                          "source": "src/canSign.ts:1-3", "function": "canSignInPerson"}],
        "step_grounding": [{"step": 2, "element": "Sign In Person", "kind": "control",
                            "source": "src/Summary.tsx", "lines": "4", "ui_text": "Sign In Person"}],
    }
    outcomes = gv._verify_expected("happy_path:0", case, index, shown=None)
    assert case["expected_verified"] is True, case.get("expected_verification_downgraded")
    assert [o.status for o in outcomes] == ["verified"]


# ── 4. a hook's typed parameter list is not its body ──────────────────────────

HOOK = """export function useSignNow({
  flags,
  userInfo,
}: {
  flags?: Flags;
  userInfo: any;
}) {
  const allowed = React.useMemo(() => {
    if (!userInfo || !flags?.signNow || userInfo.isDelegate) return false;
    return signers.some((s) => s.email === userInfo.senderEmail);
  }, [userInfo, flags]);
  return { allowed };
}"""


def test_a_hooks_destructured_typed_parameters_do_not_end_the_function():
    fn = sf.function_named(sf.functions("use-sign-now.ts", HOOK), "useSignNow")
    assert (fn.start, fn.end) == (1, 13)


@pytest.mark.asyncio
async def test_a_rule_the_extractor_cannot_read_is_not_downgraded():
    gh = FakeGitHub({REPO: ["src/useSignNow.ts"]}, {REPO: {"src/useSignNow.ts": HOOK}})
    prs = [{**PRS[0], "files_changed": [{"filename": "src/useSignNow.ts", "status": "modified",
                                         "patch": "@@ +1 @@\n+x"}]}]
    index = await sf.read_source_index(gh, prs)
    names, problems = gv._verify_logic_claim(
        {"rule": "no Sign Now when userInfo is null, the flag is off, or the caller is a delegate",
         "source": "src/useSignNow.ts:1-13", "function": "useSignNow"}, index, set())
    assert problems == []


# ── 5. an absence needs only the condition that fails ─────────────────────────

def _case_with(expected: str) -> dict:
    return {
        "title": "TC on envelope history",
        "steps": ["As the TC, open the owner's envelope history"],
        "expected": expected,
        "expected_verified": True,
        "expected_source": "src/canSign.ts:1-3",
        "logic_claims": [{"rule": "the flag inPersonSigning is on and the caller is not a delegate",
                          "source": "src/canSign.ts:1-3", "function": "canSignInPerson"}],
    }


@pytest.mark.asyncio
async def test_a_negative_expectation_naming_only_the_failing_condition_stays_verified():
    index = await sf.read_source_index(_gh(), PRS)
    case = _case_with("No 'Sign In Person' link: the caller is a delegate, so canSignInPerson is false.")
    gv._verify_expected("happy_path:0", case, index, shown=None)
    assert case["expected_verified"] is True, case.get("expected_verification_downgraded")


@pytest.mark.asyncio
async def test_a_positive_expectation_must_still_name_every_condition():
    index = await sf.read_source_index(_gh(), PRS)
    case = _case_with("The owner sees 'Sign In Person': the flag is on and the caller is not a delegate.")
    gv._verify_expected("happy_path:0", case, index, shown=None)
    assert case["expected_verified"] is False
    assert "isStartSafe" in case["expected_verification_downgraded"]
