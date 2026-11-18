# SK-3060 fixture: a contradicted Expected kept beside its correction

Plan 721 v1, `edge_cases:0` ("A GFE step that fails still produces redacted
request XML in the debug ZIP"). The steps use county `Notacounty`, which fails
in county resolution — not a SOAP call — before any later step sends a request
(`src/gfe-quote-flow.ts:13-17`). The rule Q re-check said so, but prepended its
correction and kept the original "The failed step's request XML is still in the
ZIP" in the same Expected, and its 300-character slice cut the step file name to
`step2_GetCustomerConfiguredPr`.

What is reachable: a SOAP step that fails *after sending* keeps its redacted
request (`src/gfe-debug-service.ts:43-45`, re-redacted in `src/gfe-debug-zip.ts:6`),
seen on integ when FNTG answered GetGFEQuote with HasErrors "Service unavailable".

The `src/` files are short sanitized excerpts of the real files at main-c66d2f7
(names kept verbatim; hosts, SOAP action URLs, tokens and customer ids removed).
Used by `tests/test_sk3060_contradicted_expected.py`.
