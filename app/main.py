from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from app.models import GenerateRequest, HealthResponse
from app.services.jira_service import jira_service
from app.services.llm_service import llm_service
from app.utils.formatter import format_test_plan_to_markdown
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Jira Test Plan Bot",
    description="Generate QA test plans from Jira tickets and post them as comments",
    version="1.0.0"
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint to verify the service is running.
    
    Returns:
        HealthResponse with status and message
    """
    return HealthResponse(
        status="healthy",
        message="Jira Test Plan Bot is running"
    )


@app.post("/generate")
async def generate_test_plan(request: GenerateRequest):
    """
    Generate a test plan for a Jira issue and post it as a comment.
    
    Args:
        request: GenerateRequest containing the issue_key
        
    Returns:
        JSON response with success status and message
        
    Raises:
        HTTPException: If any step of the process fails
    """
    issue_key = request.issue_key
    logger.info(f"Generating test plan for issue: {issue_key}")
    
    try:
        # Step 1: Fetch Jira issue
        logger.info(f"Fetching issue {issue_key} from Jira")
        issue = jira_service.get_issue(issue_key)
        
        # Step 2: Generate test plan using LLM
        logger.info("Generating test plan with LLM")
        test_plan = llm_service.generate_test_plan(
            issue["summary"],
            issue["description"]
        )
        
        # Step 3: Format test plan to Markdown
        logger.info("Formatting test plan to Markdown")
        markdown = format_test_plan_to_markdown(test_plan, issue_key)
        
        # Step 4: Post comment to Jira
        logger.info(f"Posting test plan to Jira issue {issue_key}")
        jira_service.add_comment(issue_key, markdown)
        
        logger.info(f"Test plan generated and posted successfully for {issue_key}")
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": f"Test plan generated and posted to {issue_key}",
                "issue_key": issue_key
            }
        )
        
    except Exception as e:
        logger.error(f"Error generating test plan for {issue_key}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate test plan: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    from app.config import settings
    
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True
    )
