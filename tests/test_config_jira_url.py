from src.app.config import Settings


def test_jira_url_trailing_slash_is_stripped(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net/")
    assert Settings(_env_file=None).jira_url == "https://example.atlassian.net"


def test_jira_url_without_slash_is_unchanged(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    assert Settings(_env_file=None).jira_url == "https://example.atlassian.net"
