"""init_client turns TLS on only for URIs that use it.

Passing tlsCAFile makes pymongo use TLS, so passing it unconditionally broke the
local URI that .env.example documents: a plain mongod answers a TLS handshake
with EOF, and every query timed out with "SSL handshake failed".
"""

import pytest

from src.app.db.mongo import _uses_tls


@pytest.mark.parametrize(
    "uri, expected",
    [
        ("mongodb+srv://u:p@npe.example.mongodb.net/db?retryWrites=true", True),
        ("mongodb://localhost:27017/jira_testplan_bot", False),
        ("mongodb://localhost:27017/jira_testplan_bot?replicaSet=rs0", False),
        ("mongodb://host:27017/db?tls=true", True),
        ("mongodb://host:27017/db?ssl=true", True),
        ("mongodb://host:27017/db?TLS=True", True),
        ("mongodb://host:27017/db?tls=false", False),
    ],
)
def test_uses_tls(uri, expected):
    assert _uses_tls(uri) is expected
