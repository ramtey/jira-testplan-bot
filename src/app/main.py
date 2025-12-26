from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
)

app = FastAPI(title="Jira Test Plan Bot", version="0.1.0")

# Configure CORS for frontend communication
# NOTE: For production, update allow_origins to include your production URLs
# or configure via environment variable (e.g., settings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server default port
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/issue/{issue_key}")
async def get_issue(issue_key: str):
    jira = JiraClient()
    try:
        issue = await jira.get_issue(issue_key)
        return {
            "key": issue.key,
            "summary": issue.summary,
            "description": issue.description,
            "labels": issue.labels,
            "issue_type": issue.issue_type,
            "description_quality": {
                "has_description": issue.description_analysis.has_description,
                "is_weak": issue.description_analysis.is_weak,
                "warnings": issue.description_analysis.warnings,
                "char_count": issue.description_analysis.char_count,
                "word_count": issue.description_analysis.word_count,
            },
        }
    except JiraNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except JiraAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except JiraConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
