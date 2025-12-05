from openai import OpenAI
from app.config import settings
from app.models import TestPlan
import json
import logging

logger = logging.getLogger(__name__)


class LLMService:
    """Service for interacting with LLM API to generate test plans."""
    
    def __init__(self):
        """Initialize OpenAI client."""
        self._client = None
        self.model = settings.openai_model
    
    @property
    def client(self):
        """Lazy initialization of OpenAI client."""
        if self._client is None:
            self._client = OpenAI(api_key=settings.openai_api_key)
        return self._client
    
    def generate_test_plan(self, issue_summary: str, issue_description: str) -> TestPlan:
        """
        Generate a test plan using LLM.
        
        Args:
            issue_summary: The Jira issue summary
            issue_description: The Jira issue description
            
        Returns:
            TestPlan object with structured test plan data
            
        Raises:
            Exception: If test plan generation fails
        """
        prompt = self._create_prompt(issue_summary, issue_description)
        
        try:
            # Prepare API call parameters
            api_params = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a QA engineer expert. Generate comprehensive test plans in JSON format."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "temperature": 0.7
            }
            
            # Add response_format if supported (OpenAI API 1.0+)
            try:
                api_params["response_format"] = {"type": "json_object"}
                response = self.client.chat.completions.create(**api_params)
            except TypeError:
                # Fallback for older API versions that don't support response_format
                logger.warning("response_format not supported, falling back to text response")
                del api_params["response_format"]
                response = self.client.chat.completions.create(**api_params)
            
            result = json.loads(response.choices[0].message.content)
            logger.info("Test plan generated successfully")
            return TestPlan(**result)
            
        except Exception as e:
            logger.error(f"Error generating test plan: {str(e)}")
            raise
    
    def _create_prompt(self, summary: str, description: str) -> str:
        """Create the prompt for LLM."""
        return f"""
Generate a comprehensive test plan for the following Jira ticket in JSON format:

**Summary:** {summary}

**Description:** {description}

Please provide a JSON object with the following structure:
{{
  "happy_path": [
    {{
      "scenario": "Brief description of the scenario",
      "steps": ["Step 1", "Step 2", "Step 3"],
      "expected_result": "What should happen"
    }}
  ],
  "edge_cases": [
    {{
      "scenario": "Edge case description",
      "steps": ["Step 1", "Step 2"],
      "expected_result": "Expected behavior"
    }}
  ],
  "regression_checklist": [
    "Item 1 to verify",
    "Item 2 to verify"
  ],
  "questions": [
    "Question 1 for clarification",
    "Question 2 about requirements"
  ]
}}

Ensure the test plan is thorough, covering:
1. Happy path scenarios (typical user flows)
2. Edge cases (boundary conditions, error scenarios)
3. Regression checklist (existing functionality to verify)
4. Questions (clarifications needed from the team)
"""


# Singleton instance
llm_service = LLMService()
