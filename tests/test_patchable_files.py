"""Which changed files reach the generator as diffs.

Why this file exists
--------------------
`_is_patchable_file` decides which PR diffs the generator ever sees, and a
language it does not list is silently invisible. Until 2026-10-07 `.cs` was
missing, so SK-2687's two .NET PRs (#92, #96) reached the prompt as a bare file
list: every API case hedged its status code as "unverified" while the handler
that returns `403 "Only the envelope's owner…"` was one fetch away.

Excluding test code matters just as much once those languages are in: a .NET
PR's test project is routinely larger than the change it tests, and would eat
the diff budget before the handler is shown.
"""

from __future__ import annotations

import pytest

from src.app.jira_client import _is_patchable_file

# Real paths from SK-2687's PRs.
HANDLER = ("DigiSign.Sender.Envelope.Application/Requests/Commands/Signer/"
           "StartInPersonSession/StartInPersonSessionHandler.cs")
HANDLER_TESTS = ("DigiSign.Sender.Envelope.Application.Tests/Requests/Commands/Signer/"
                 "StartInPersonSession/StartInPersonSessionHandlerTests.cs")


@pytest.mark.parametrize("path", [
    HANDLER,
    "DigiSign.Sender.Envelope.Application/Controllers/SignerController.cs",
    "DigiSign.Sender.Envelope.Common/RequestAccessor/RequestAccessor.cs",
    "packages/sender/src/components/envelopeManagement/ActionsMenu.tsx",
    "src/main/java/com/skyslope/Envelope.java",
    "internal/envelope/handler.go",
    "app/models/envelope.rb",
    # Names that end in "test"/"tests" without being tests.
    "DigiSign.Sender.Envelope.Domain/Latest.cs",
    "src/main/java/com/skyslope/Contest.java",
    "DigiSign.Sender.Envelope.Common/Requests.cs",
])
def test_runtime_source_is_diffed(path):
    assert _is_patchable_file(path)


@pytest.mark.parametrize("path", [
    HANDLER_TESTS,
    "DigiSign.Sender.Envelope.Common.Tests/RequestAccessor/ActingUserHeaderTests.cs",
    "Foo.UnitTests/Thing.cs",                      # test project, not a *Tests.cs name
    "src/test/java/com/skyslope/EnvelopeTest.java",
    "internal/envelope/handler_test.go",
    "spec/models/envelope_spec.rb",
    "packages/sender/src/common/inPersonSigning/use-can-sign-in-person.test.ts",
    "packages/sender/src/__tests__/Summary.tsx",
])
def test_test_code_is_not_diffed(path):
    assert not _is_patchable_file(path)


@pytest.mark.parametrize("path", [
    "docs/threat-model.md",
    "digisign3/sender-ui/integ.integ-west/values.yaml",
    "packages/sender/vite.config.ts",
])
def test_non_runtime_files_are_not_diffed(path):
    assert not _is_patchable_file(path)
