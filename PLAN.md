# Shared-component fan-out rule

## Why the rule exists

Bot-generated test plans faithfully test what the AC asks for. When a
story adds behaviour to a component that fans out across multiple
calculator roles, and the AC does not distinguish between those roles,
the plan reads as if the change applies uniformly — and passes.

**Worked failure — SK-1898 → SK-2511.**  SK-1898 asked us to "display
assessed value and loan amount in the Property Tax Results modal." The
modal is a shared component consumed by both the buyer estimate and the
seller net sheet. Loan amount is meaningful on the seller side (it feeds
mortgage-balance amortization) but meaningless on the buyer side (the
buyer calculator explicitly does not consume it). The AC did not
distinguish between roles, so the bot-generated plan verified only
"field renders when data exists" and passed. Six months later the
misplaced loan-amount field on the buyer side was reported as bug
SK-2511.

The plan needed to fan out to *both* roles and, for each role, assert
the *absence* of any field the role does not consume. That's what this
rule adds.

## When the rule fires

A ticket needs the fan-out when **both** of the following hold:

1. **Role is unspecified.** No summary/description/AC text scopes the
   change to a single role — no `"Seller net sheet: …"` title prefix,
   no `"As a seller I want …"` user story, no `"buyer-side"` /
   `"only sellers"` qualifier.
2. **A shared component is implicated.** Any of:
   - The linked PR touches a file under one of the shared-component
     paths in `SHARED_COMPONENT_DIRS`
     (default: `apps/expo/src/components/modals/`,
     `apps/expo/src/components/shared/`,
     `packages/engine/src/components/shared/`,
     `packages/ui/src/`).
   - The ticket text uses shared-component language
     (`"shared component"`, `"shared modal"`, `"results modal"`,
     `"results view"`).
   - The ticket names a field that the `ROLE_CONSUMPTION_MAP` says has
     role-divergent consumption (e.g. loan amount is consumed by the
     seller net sheet but not the buyer estimate).

## What the fan-out demands of the LLM

When the rule fires, an extra guidance block is appended to the plan
prompt in [src/app/llm_client.py](src/app/llm_client.py) via
[src/app/shared_component_fanout.py](src/app/shared_component_fanout.py).
The block instructs the LLM to:

- Emit at least one `happy_path` case per role in
  `KNOWN_ROLES` (currently: `buyer estimate`, `seller net sheet`).
- For each role, emit an explicit negative-space assertion listing the
  fields introduced by this ticket that the role does NOT consume.
- Title cases with role markers (`[Buyer estimate] …`,
  `[Seller net sheet] …`) so QA can read the fan-out without opening
  the JSON tags.
- Include the SK-1898 → SK-2511 story verbatim as an anchor for the
  failure mode.

## The consumption map

`ROLE_CONSUMPTION_MAP` in
[src/app/shared_component_fanout.py](src/app/shared_component_fanout.py)
is a hand-curated table: field label → the subset of `KNOWN_ROLES` that
consume the field per the target repo's calculators (source of truth is
`packages/engine/src/calculators/{buyerNetSheet,sellerNetSheet}.ts` in
the agent-calculator repo, specifically their `mapPropertyInfo`
functions).

**Why static, not fetched from GitHub at generate-time.** The static
table is small, reviewable, and unit-testable. It does not burn GitHub
API budget on every ticket, and it doesn't add a runtime failure mode
(unreachable repo, parse error). When we add a new shared field that
diverges by role, one small PR against this file adds coverage
immediately.

**When a field is not in the map.** The fan-out still fires (via the
shared-component-dir signal), and the guidance instructs the LLM to
emit the negative-space check *diagnostically* for that field —
"capture whether it renders on this surface and flag for PM." It does
NOT ask the LLM to assert absence, because doing so would pin the
plan's pass criterion to whatever the app currently shows, and the
current output on a genuinely ambiguous field is the prime suspect —
not the answer key. This matches the derived-value-from-spec rule
enforced elsewhere in the prompt.

## The escape hatch

If the ticket clearly scopes itself to one role, the detector returns
`None` and no guidance is added. The `_ROLE_SCOPING_PATTERNS` in
`shared_component_fanout.py` codify what counts as clear scoping.
The LLM also has an in-prompt escape hatch: if it has strong evidence
from the ticket or diff that the change applies to only one role, it
may emit a single-role plan, but must add a `grounding_warnings`
entry naming the evidence it relied on — so a reviewer can second-guess
a thin justification.

## How to extend the rule

- **New shared field.** Add a row to `ROLE_CONSUMPTION_MAP` with the
  roles that consume the field. Verify by reading the calculator
  functions in the target repo, not by observing app behaviour.
- **New shared-component directory.** Append to
  `SHARED_COMPONENT_DIRS`.
- **New consuming role.** Add it to `KNOWN_ROLES` and update every
  existing row in `ROLE_CONSUMPTION_MAP` to state whether the new role
  consumes each field.

The regression test at
[tests/test_shared_component_fanout.py](tests/test_shared_component_fanout.py)
anchors the SK-1898 → SK-2511 case: an SK-1898-shaped ticket must
produce a prompt with the fan-out block AND, when the LLM follows that
guidance, a plan with per-role cases and the negative-space assertion
on loan amount for the buyer side.
