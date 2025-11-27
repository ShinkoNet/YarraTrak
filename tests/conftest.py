"""
Pytest configuration and fixtures.
"""

import os
import pytest
from dotenv import load_dotenv

# Load .env file before any tests run
load_dotenv()


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "live_api: marks tests that require live API calls (deselect with '-m \"not live_api\"')"
    )


@pytest.fixture(scope="session")
def groq_api_available():
    """Check if GROQ_API_KEY is available."""
    return bool(os.getenv("GROQ_API_KEY"))


@pytest.fixture
def skip_without_groq(groq_api_available):
    """Skip test if GROQ_API_KEY is not set."""
    if not groq_api_available:
        pytest.skip("GROQ_API_KEY not set - skipping live API test")
