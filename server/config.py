import os
from dotenv import load_dotenv

load_dotenv()

PTV_DEV_ID = os.getenv("PTV_DEV_ID")
PTV_API_KEY = os.getenv("PTV_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")

# API Keys for authentication - comma-separated list in .env
# If empty, all endpoints are open (dev mode)
API_KEYS = set(filter(None, os.getenv("API_KEYS", "").split(",")))

# Set to False to disable the guardrail agent (allows small talk, less restricted)
ENABLE_GUARDRAIL = False