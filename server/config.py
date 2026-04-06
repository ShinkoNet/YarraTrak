import os
from dotenv import load_dotenv

load_dotenv()

PTV_DEV_ID = os.getenv("PTV_DEV_ID")
PTV_API_KEY = os.getenv("PTV_API_KEY")


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


ALLOWED_ORIGINS = _get_csv("ALLOWED_ORIGINS")
HTTP_QUERY_RATE_LIMIT = _get_int("HTTP_QUERY_RATE_LIMIT", 12)
HTTP_FAVOURITE_RATE_LIMIT = _get_int("HTTP_FAVOURITE_RATE_LIMIT", 60)
WS_QUERY_RATE_LIMIT = _get_int("WS_QUERY_RATE_LIMIT", 12)
MAX_WS_CONNECTIONS_PER_IP = _get_int("MAX_WS_CONNECTIONS_PER_IP", 3)
MAX_FAVOURITE_BUTTONS = _get_int("MAX_FAVOURITE_BUTTONS", 10)
MAX_QUERY_LENGTH = _get_int("MAX_QUERY_LENGTH", 300)
