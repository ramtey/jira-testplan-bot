# jira-testplan-bot

Generate a structured QA test plan from a Jira ticket (title + description / acceptance criteria) and post it back to the ticket as a comment.

## MVP Scope

### What it does (now)
- Runs a small FastAPI service
- (Next milestone) Fetches a Jira issue by key and generates a test plan using an LLM
- (Next milestone) Posts the generated plan back to Jira as a comment

### What it does NOT do yet
- Auto-trigger on "Ready to Test" (webhooks/automation)
- GitHub PR/commit analysis
- Attachment parsing
- UI

## Tech Stack

- Python
- FastAPI
- uv for dependency management + running
- httpx for HTTP calls
- Pydantic Settings for environment config

## Project Structure

```
src/
  app/
    main.py        # FastAPI app entrypoint
    config.py      # environment configuration
tests/
```

## Prerequisites

- Python 3.11+ recommended
- uv installed

### Install uv

```bash
pip install uv
```

## Setup

1. Install dependencies:

```bash
uv sync
```

2. Create your local env file:

```bash
cp .env.example .env
```

3. Fill in `.env` values (do not commit `.env`):
   - `JIRA_BASE_URL`
   - `JIRA_EMAIL`
   - `JIRA_API_TOKEN`
   - (later) `LLM_API_KEY`

## Run the API locally

```bash
uv run uvicorn src.app.main:app --reload
```

### Health check

```
http://127.0.0.1:8000/health
```

### API docs (Swagger)

```
http://127.0.0.1:8000/docs
```

## Environments & Secrets

- Never commit `.env`
- Use `.env.example` as the template

## Roadmap (high level)

- [ ] Jira: fetch issue summary/description by issue key
- [ ] LLM: generate structured JSON test plan
- [ ] Formatter: convert JSON to Jira-friendly Markdown
- [ ] Jira: post test plan as a comment
- [ ] Add idempotency (avoid duplicate comments)
- [ ] Optional: webhook trigger on "Ready to Test"
- [ ] Optional: GitHub PR/file-change awareness

## License

See [LICENSE](LICENSE).
