from fastapi import FastAPI

app = FastAPI(title="Jira Test Plan Bot", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}
