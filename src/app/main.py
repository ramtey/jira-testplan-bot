from fastapi import FastAPI, HTTPException

from .jira_client import (
    JiraAuthError,
    JiraClient,
    JiraConnectionError,
    JiraNotFoundError,
)

app = FastAPI(title="Jira Test Plan Bot", version="0.1.0")


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
