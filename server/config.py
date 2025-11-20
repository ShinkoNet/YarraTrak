import os
from dotenv import load_dotenv

load_dotenv()

PTV_DEV_ID = os.getenv("PTV_DEV_ID")
PTV_API_KEY = os.getenv("PTV_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")