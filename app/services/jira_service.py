from jira import JIRA
from app.config import settings
import logging

logger = logging.getLogger(__name__)

# Constants
DEFAULT_DESCRIPTION = "No description provided"


class JiraService:
    """Service for interacting with Jira API."""
    
    def __init__(self):
        """Initialize Jira client with authentication."""
        self._jira = None
    
    @property
    def jira(self):
        """Lazy initialization of Jira client."""
        if self._jira is None:
            self._jira = JIRA(
                server=settings.jira_url,
                basic_auth=(settings.jira_email, settings.jira_api_token)
            )
        return self._jira
    
    def get_issue(self, issue_key: str) -> dict:
        """
        Fetch issue details from Jira.
        
        Args:
            issue_key: The Jira issue key (e.g., 'PROJ-123')
            
        Returns:
            Dictionary with issue summary and description
            
        Raises:
            Exception: If issue cannot be fetched
        """
        try:
            issue = self.jira.issue(issue_key)
            return {
                "key": issue_key,
                "summary": issue.fields.summary,
                "description": issue.fields.description or DEFAULT_DESCRIPTION
            }
        except Exception as e:
            logger.error(f"Error fetching issue {issue_key}: {str(e)}")
            raise
    
    def add_comment(self, issue_key: str, comment: str) -> None:
        """
        Add a comment to a Jira issue.
        
        Args:
            issue_key: The Jira issue key
            comment: The comment text to add
            
        Raises:
            Exception: If comment cannot be added
        """
        try:
            self.jira.add_comment(issue_key, comment)
            logger.info(f"Comment added to issue {issue_key}")
        except Exception as e:
            logger.error(f"Error adding comment to {issue_key}: {str(e)}")
            raise


# Singleton instance
jira_service = JiraService()
