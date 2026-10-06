---
# Copy into your RISK_PROFILES_DIR (outside this repo) as <product>.md and
# replace everything below with your team's knowledge.
name: Acme Docs
match:
  # A linked PR whose owner/repo contains any of these
  repos: ["acme/docs-api", "acme/docs-ui"]
  # ...or a summary matching any of these (case-insensitive regex)
  summary: ['\[Docs\]', '\bdocument editor\b']
  # Summary patterns that rule the ticket out (a repo match still wins)
  exclude_summary: ['^\s*\[Billing\]']
---
# Acme Docs risk profile

Everything below is handed to the generator as written, for every matched
ticket. Cite the bug or thread behind each line: the replay eval
(evals/replay_bounces.py, EVAL_RISK_PROFILES_DIR) holds out lines citing the
ticket it replays, so an uncited line can't be measured honestly.

Ids matter: triggers are `### T<n>.` or `**T<n><letter>.`, sections are
`## <n>.`. Cases the model takes from the profile are tagged with these ids,
and a tag naming an id that doesn't exist is ignored.

## 1. User types

Used only when a trigger below that names them fires.

| User type | What's different | Evidence |
|---|---|---|
| **Viewer** (read-only share) | Cannot edit; must not see the owner's other documents | ACME-101 |
| **Org admin** | Sees every document in the org, including private ones | ACME-140 |

## 2. Triggers → what else to test

### T1. Who-has-access code
Signal: the diff touches sharing, grants or permission checks.
- Revoking a share removes access immediately, checked with a clean session (ACME-212)
- Comment notifications still reach the owner after a share (ACME-230)

### T2. Search
Signal: the diff touches search, pickers or people lookups.
- Which accounts can be found, per user type — right people in, wrong people out (ACME-301)

## 3. Environments
- Signups are disabled in staging: any step needing a new account is written as BLOCKED, with the reason (ACME-410)

## 4. Escaped bugs log

| Date | Ticket | Escaped | Trigger |
|---|---|---|---|
| 2026-01 | ACME-212 | Revoked share still opened from a cached link | T1 |
