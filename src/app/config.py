from pydantic_settings import BaseSettings, SettingsConfigDict

from .model_capabilities import DEFAULT_CLAUDE_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Jira
    jira_url: str = ""
    jira_username: str = ""
    jira_api_token: str = ""
    # Story Points custom field id. Many Jira instances expose it as
    # customfield_10004; others (esp. newer Jira Cloud) use customfield_10016.
    # Check /rest/api/3/field on your instance and override via env.
    jira_story_points_field: str = "customfield_10004"

    # LLM configuration
    llm_provider: str = "claude"  # "claude" (recommended) or "ollama"
    # Claude default lives in model_capabilities.py alongside the per-model
    # request quirks it has to move with. Override for Ollama (llama3.1,
    # qwen2.5, ...) or to pin an older Claude.
    llm_model: str = DEFAULT_CLAUDE_MODEL
    anthropic_api_key: str | None = None  # For Claude API (required when using Claude)
    ollama_base_url: str = "http://localhost:11434"  # Ollama server URL (only needed if using Ollama)
    # Read timeout (seconds) for Claude test-plan generation. Large parents
    # (e.g. an Epic + many subtasks) push the prompt big enough that Opus can
    # spend several minutes producing 16k output tokens; 120s would surface as
    # "Claude API request timed out" mid-generation.
    claude_api_timeout_seconds: float = 600.0

    # GitHub (for PR diff fetching - Phase 3a)
    github_token: str | None = None  # GitHub personal access token (optional - enables PR diff fetching)

    # Third-pass critic that re-checks AC-grounding warnings against the linked
    # repo's actual source. When True and a github_token is available, an
    # AC-critic warning whose behaviour is present in code gets downgraded from
    # WARN to INFO so QA doesn't chase a false positive. Set to False to skip
    # the extra GitHub search + LLM round-trip (~2s + ~1s per flagged case).
    code_grounding_recheck_enabled: bool = True

    # Pre-plan deliverable classifier + post-plan surface-mismatch critic.
    # Adds one LLM round-trip BEFORE plan generation to name what the ticket
    # actually changes and where a tester observes that change, then a second
    # round-trip AFTER generation to badge cases that don't touch that surface.
    # Off by default while the prototype is validated against SK-2546-shape
    # tickets (asset uploads, config flips, doc changes where the current
    # planner defaults to "compare against the running app").
    surface_classifier_enabled: bool = False

    # Bug Lens repo hints: maps a regex pattern (matched against summary + description + comments)
    # to one or more "owner/repo" strings to search when the ticket has no explicit GitHub links.
    # Set via env as JSON, e.g. BUG_LENS_REPO_HINTS='{"title.?rep|folders": ["acme/mobile-app"]}'
    bug_lens_repo_hints: dict[str, list[str]] = {}

    # Figma (for design context - Phase 5)
    figma_token: str | None = None  # Figma personal access token (optional - enables design context)

    # Slack (for resolving Slack message links in Jira descriptions/comments)
    slack_user_token: str | None = None  # Slack user token (xoxp-) - required scopes: channels:history, groups:history, im:history, mpim:history, users:read

    # App
    app_env: str = "local"

    # ---- Your team -----------------------------------------------------
    # Everything in this block is specific to whoever is running the bot, so
    # it lives in config rather than in code: a clone that inherited one
    # team's Jira projects and coworkers could only ever be wrong for
    # everyone else.

    # QA workflow buttons (Pull-to-Testing / Pass-to-UAT / Fail-back) only show
    # for tickets whose key starts with one of these prefixes. Empty list
    # disables the workflow UI entirely. Set via env as JSON, e.g.
    # WORKFLOW_PROJECT_PREFIXES='["SK","SL"]'.
    workflow_project_prefixes: list[str] = []

    # GitHub login -> [Jira accountId, display name], for picking who a
    # fail-back goes back to. The bot finds the PR author with the most
    # changed lines and has to turn that GitHub login into a Jira user; this
    # map is tried first because the fallback search has two holes — GitHub
    # display names diverge from Jira ones, and commits routed through GitHub
    # noreply addresses resolve to nobody. Optional: without it the fallback
    # chain (commit email -> profile email -> profile name -> login) still
    # runs, it just misses more often. Set via env as JSON, e.g.
    # TEAM_GITHUB_LOGIN_TO_JIRA='{"octocat": ["557058:1234", "Octo Cat"]}'.
    team_github_login_to_jira: dict[str, tuple[str, str]] = {}

    # Jira display names of service/bot accounts that must never be left as
    # the assignee on a pass-to-UAT or a fail-back. Compared
    # case-insensitively. Belt-and-braces for the accountId-based check, which
    # a stale credential or an unfamiliar bot account can slip past. Set via
    # env as JSON, e.g. BOT_DISPLAY_NAMES='["testing acme","ci bot"]'.
    bot_display_names: list[str] = []

    # ---- Queue watcher -------------------------------------------------
    # Pre-generates a test plan the moment a ticket lands in the QA queue,
    # so the tester opens a ticket that already has a plan, critics run and
    # all. Runs as a separate process (`testplan watch`), never on its own
    # from the API.
    #
    # Projects to sweep. Empty falls back to workflow_project_prefixes so
    # there's one place to name the team's projects.
    watch_projects: list[str] = []
    # The status that means "landed in the QA queue" — the trigger moment.
    watch_status: str = "Ready to Test"
    # Seconds between sweeps. Jira's own board moves on human timescales,
    # so polling faster than this buys nothing but API calls.
    watch_interval_seconds: int = 300
    # Hard cap on plans generated per sweep. The watcher spends real money
    # with no human in the loop; this bounds a surprise (a sprint's worth of
    # tickets moved to Ready to Test at once) to a known cost.
    watch_max_per_cycle: int = 5
    # Require at least one MERGED pull request. Merge state, not mere
    # existence: a plan written against an open PR describes code that is
    # still changing, and because the watcher never regenerates, that plan
    # would outlive the code it was written from. A ticket with no diff at
    # all is separately the thin plan QA would rather write by hand.
    watch_require_merged_pr: bool = True
    # Don't re-attempt a ticket whose last attempt (success OR failure) was
    # within this window. Stops a ticket that fails every cycle — a timeout,
    # a malformed description — from burning a call every interval.
    watch_retry_cooldown_hours: int = 6

    # Database (Neon Postgres)
    database_url: str | None = None


settings = Settings()

# Issue types that don't require test plans
NON_TESTABLE_ISSUE_TYPES = {
    "Epic",
    "Spike",
}
