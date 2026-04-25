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


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


ALLOWED_ORIGINS = _get_csv("ALLOWED_ORIGINS")
HTTP_QUERY_RATE_LIMIT = _get_int("HTTP_QUERY_RATE_LIMIT", 12)
HTTP_FAVOURITE_RATE_LIMIT = _get_int("HTTP_FAVOURITE_RATE_LIMIT", 60)
WS_QUERY_RATE_LIMIT = _get_int("WS_QUERY_RATE_LIMIT", 12)
MAX_WS_CONNECTIONS_PER_IP = _get_int("MAX_WS_CONNECTIONS_PER_IP", 3)
MAX_FAVOURITE_BUTTONS = _get_int("MAX_FAVOURITE_BUTTONS", 10)
MAX_QUERY_LENGTH = _get_int("MAX_QUERY_LENGTH", 300)
FAVOURITE_CACHE_TTL_SECONDS = _get_float("FAVOURITE_CACHE_TTL_SECONDS", 45.0)
FAVOURITE_FETCH_CONCURRENCY = _get_int("FAVOURITE_FETCH_CONCURRENCY", 20)
METRICS_LOG_INTERVAL_SECONDS = _get_float("METRICS_LOG_INTERVAL_SECONDS", 60.0)
HTTP_CLIENT_TIMEOUT_SECONDS = _get_float("HTTP_CLIENT_TIMEOUT_SECONDS", 10.0)
HTTP_CLIENT_MAX_CONNECTIONS = _get_int("HTTP_CLIENT_MAX_CONNECTIONS", 40)
HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS = _get_int("HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS", 20)
PUBLIC_APPSTORE_URL = _get_str("PUBLIC_APPSTORE_URL", "https://apps.repebble.com/")
OPENROUTER_BASE_URL = _get_str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = _get_str("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
PUBLIC_BASE_HOST = _get_str("PUBLIC_BASE_HOST", "ptv.netcavy.net").lower()
INTERNAL_DASHBOARD_HOST = _get_str("INTERNAL_DASHBOARD_HOST", "ptv.yourinternal.website").lower()
METRICS_HISTORY_INTERVAL_SECONDS = _get_float("METRICS_HISTORY_INTERVAL_SECONDS", 60.0)
METRICS_HISTORY_MAX_POINTS = _get_int("METRICS_HISTORY_MAX_POINTS", 10080)
