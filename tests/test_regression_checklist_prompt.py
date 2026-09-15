"""The prompt's rules about regression-checklist lines, and about how much
the testID reference is allowed to prove.

Two prompt-level halves of the SK-2342 fix, both easy to lose in a later
edit of a 5000-line prompt builder and neither visible in any other test:

1. The regression checklist has its own grounding rule with an escape hatch
   that works for a bare string. The UI GROUNDING rules bind "every UI
   element you name in a *test step*", and their remedy is three operations
   on a case's JSON object — set `needs_manual_verification`, add a
   `grounding_warnings` entry, key its `ac_id` to the case's `covers_acs`.
   A checklist line has no steps, no JSON object and no covers_acs, so a
   model that cannot ground a line has no sanctioned way to say so. It
   writes the line clean, which is how plan 522 shipped a "Text" share
   option and a seek control with `grounding_warnings: null`.

2. The testID reference is no longer described as exhaustive, because it
   isn't. Its generator extracts conditional testIDs with a regex whose
   `[^}]*` stops at the first `}` — inside a template literal that's the one
   closing `${...}` — so `testID={isLoading ? ... : ...}` yields nothing and
   real shipping controls are missing from the file. Telling the model
   "not listed means it does not exist" licenses deleting a control that
   does.
"""

from src.app.llm_client import SYSTEM_PROMPT, LLMClient


# The checklist rules live in SYSTEM_PROMPT (always sent, every provider);
# the testID-reference block is assembled per ticket in _build_prompt, because
# it only exists when the repo context carried a reference.
def _plan_prompt(**kwargs) -> str:
    """The single-ticket user prompt for a plain ticket."""
    return LLMClient._build_prompt(
        LLMClient,
        ticket_key="SK-2342",
        summary="Preview the audio walkthrough in English or Spanish",
        description="AC1: The agent can preview the walkthrough in Spanish.",
        testing_context={},
        **kwargs,
    )


def _prompt_with_testid_reference() -> str:
    return _plan_prompt(
        development_info={
            "pull_requests": [{"number": 1, "repository": "acme/app"}],
            "repository_context": {
                "testid_reference": (
                    "### DockedAudioPlayer\n- `{dynamic}-language-button`"
                ),
            },
        },
    )


# ─── the checklist grounding rule ─────────────────────────────────────────────


def test_the_checklist_rules_forbid_naming_an_unsourced_control():
    prompt = SYSTEM_PROMPT
    assert "DO NOT NAME A CONTROL THAT ISN'T IN YOUR SOURCES" in prompt


def test_the_checklist_rules_carry_both_sk2342_lines_as_examples():
    """The two real failures, verbatim enough to be recognisable. A rule
    stated abstractly is the rule that was already there and already
    produced these lines."""
    prompt = SYSTEM_PROMPT
    assert "Text, Print and Download share options" in prompt
    assert "play/pause/seek" in prompt


def test_the_checklist_rules_give_a_string_shaped_escape_hatch():
    """The point of the block: when a control can't be sourced, describe
    the surface and the observable behaviour instead of the control. That
    keeps the line gradeable AND keeps the coverage — which is what the
    case-object remedy (a badge + a warning) cannot do for a string."""
    prompt = SYSTEM_PROMPT
    assert "nowhere to carry" in prompt
    assert "needs_manual_verification" in prompt
    assert "instead of the control" in prompt


def test_the_checklist_rules_ban_hedging_as_a_way_out():
    """"the Text share option, if present, still works" is the same
    ungradeable line with an apology attached, and it is the obvious thing
    a model reaches for when told to be careful."""
    prompt = SYSTEM_PROMPT
    assert "if present" in prompt


def test_the_checklist_rules_still_demand_breadth():
    """The anti-over-correction guard at the prompt level. Dropping a line
    stays the LAST resort, allowed only when the surface can't be named
    either — otherwise a model reading the new rule would prune the
    checklist down to what it can prove, which is the worse bug."""
    prompt = SYSTEM_PROMPT
    assert "Only drop the line entirely if you cannot name the surface either" in prompt
    assert "Breadth is the point of this" in prompt


# ─── the testID reference's evidential weight ─────────────────────────────────


def test_the_testid_reference_is_not_called_exhaustive():
    """The claim was false. It licensed "absent from the file, therefore
    absent from the app", which is exactly the inference that would cut a
    real control out of a checklist line."""
    prompt = _prompt_with_testid_reference()
    assert "EXHAUSTIVE" not in prompt


def test_presence_in_the_reference_still_proves_a_control_exists():
    """Softening the claim must not cost the rule its teeth in the
    direction where it IS sound."""
    prompt = _prompt_with_testid_reference()
    assert "If an element IS listed, it exists" in prompt


def test_absence_from_the_reference_sends_the_model_to_the_diff():
    prompt = _prompt_with_testid_reference()
    assert "reason to look for it in the PR diff" in prompt
    assert "do NOT invent steps for it" in prompt
