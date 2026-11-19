# SK-3014 fixture: an Expected that asserts an internal path the steps never take

Plan 734 v1, `edge_cases:4` ("A refused first message on a brand-new UUID creates
the row with the right owner and saved messages"). Its Expected ended with
"The RPC created the row first, and upsertStreamedText then took the 23505 →
update path with no error." A black-box tester cannot see which branch ran, and
the claim is false for the case's own steps:

- `upsertStreamedText` reads the row first (`src/streamedTextsRepository.ts:114`);
- record_chat_refusal has already created it, so it goes straight to the
  owner-scoped update (`:139-150`);
- the `UNIQUE_VIOLATION` (`'23505'`, `:11`) fallback (`:128-135`) runs only when
  another write creates the row between that check and the insert — a race one
  sequential first message never produces.

`src/streamedTextsRepository.ts` is the real file at 78690e84 with
`getAllStreamedTexts` (lines 14-79) left out and padded, so every line number
above is the real one. Used by `tests/test_sk3014_observable_expected.py`.
