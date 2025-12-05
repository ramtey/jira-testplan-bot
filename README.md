# Jira Test Plan Bot

A FastAPI-based service that automatically generates comprehensive QA test plans from Jira tickets using AI (LLM) and posts them back as comments.

## Features

- 🚀 FastAPI-based REST API
- 📋 Fetches Jira ticket details (summary + description)
- 🤖 Uses LLM (OpenAI) to generate structured test plans
- ✨ Generates test plans including:
  - Happy path test cases
  - Edge cases
  - Regression checklist
  - Clarification questions
- 📝 Formats test plans in Markdown
- 💬 Posts test plans as Jira comments automatically
- ❤️ Health check endpoint

## Project Structure

```
jira-testplan-bot/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration settings
│   ├── models/
│   │   └── __init__.py      # Pydantic models
│   ├── services/
│   │   ├── __init__.py
│   │   ├── jira_service.py  # Jira API integration
│   │   └── llm_service.py   # LLM integration
│   └── utils/
│       ├── __init__.py
│       └── formatter.py     # Markdown formatter
├── requirements.txt         # Python dependencies
├── .env.example            # Environment variables template
├── example_usage.py        # Example API usage script
├── Dockerfile              # Docker container definition
├── docker-compose.yml      # Docker Compose configuration
└── README.md              # Project documentation
```

## Prerequisites

- Python 3.8+
- Jira account with API access
- OpenAI API key

## Setup

### Option 1: Local Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/ramtey/jira-testplan-bot.git
   cd jira-testplan-bot
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**
   
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` and fill in your credentials:
   ```env
   # Jira Configuration
   JIRA_URL=https://your-instance.atlassian.net
   JIRA_EMAIL=your-email@example.com
   JIRA_API_TOKEN=your-jira-api-token
   
   # LLM Configuration (OpenAI)
   OPENAI_API_KEY=your-openai-api-key
   OPENAI_MODEL=gpt-4
   
   # Application Configuration
   APP_HOST=0.0.0.0
   APP_PORT=8000
   ```

### Option 2: Docker Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/ramtey/jira-testplan-bot.git
   cd jira-testplan-bot
   ```

2. **Configure environment variables**
   
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   
   Edit `.env` and fill in your credentials.

3. **Build and run with Docker Compose**
   ```bash
   docker-compose up -d
   ```

4. **Check logs**
   ```bash
   docker-compose logs -f
   ```

5. **Stop the service**
   ```bash
   docker-compose down
   ```

### Getting Jira API Token

1. Log in to Jira
2. Go to https://id.atlassian.com/manage-profile/security/api-tokens
3. Click "Create API token"
4. Give it a name and copy the generated token

### Getting OpenAI API Key

1. Sign up at https://platform.openai.com/
2. Go to API keys section
3. Create a new API key

## Usage

### Start the Server

**Option 1: Local Python**
```bash
python -m app.main
```

Or using uvicorn directly:
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Option 2: Docker**
```bash
docker-compose up -d
```

The server will start at `http://localhost:8000`

### Example Usage Script

Use the provided example script to test the API:

```bash
# Check server health
python example_usage.py

# Generate test plan for a specific issue
python example_usage.py PROJ-123
```

### API Endpoints

#### Health Check

**GET** `/health`

Check if the service is running.

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "healthy",
  "message": "Jira Test Plan Bot is running"
}
```

#### Generate Test Plan

**POST** `/generate`

Generate a test plan for a Jira issue and post it as a comment.

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"issue_key": "PROJ-123"}'
```

Response:
```json
{
  "success": true,
  "message": "Test plan generated and posted to PROJ-123",
  "issue_key": "PROJ-123"
}
```

### API Documentation

Once the server is running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Test Plan Format

The generated test plan includes:

1. **Happy Path Test Cases**: Normal user flow scenarios with steps and expected results
2. **Edge Cases**: Boundary conditions and error scenarios
3. **Regression Checklist**: Existing functionality to verify
4. **Questions**: Clarifications needed from the team

Example output (posted as Jira comment):

```markdown
# Test Plan for PROJ-123

_Auto-generated test plan_

---

## 🎯 Happy Path Test Cases

### Test Case 1: User login with valid credentials
**Steps:**
1. Navigate to login page
2. Enter valid username and password
3. Click login button

**Expected Result:** User is successfully logged in and redirected to dashboard

## ⚠️ Edge Cases

### Edge Case 1: Login with invalid password
**Steps:**
1. Navigate to login page
2. Enter valid username but invalid password
3. Click login button

**Expected Result:** Error message displayed, user remains on login page

## ✅ Regression Checklist

- [ ] Existing users can still log in
- [ ] Password reset functionality works
- [ ] Session timeout works correctly

## ❓ Questions for Clarification

1. What is the maximum login attempt limit?
2. Should we implement CAPTCHA after failed attempts?

---
_Generated automatically by Jira Test Plan Bot_
```

## Development

### Running with reload

```bash
uvicorn app.main:app --reload
```

### Logging

The application logs all operations to stdout with INFO level by default.

## Error Handling

The API returns appropriate HTTP status codes:
- `200`: Success
- `500`: Internal server error (with error details)

## Security Notes

- Never commit `.env` file
- Keep your API tokens secure
- Use environment variables for all sensitive data
- Consider using a secrets manager in production

## License

This project is licensed under the MIT License - see the LICENSE file for details.
