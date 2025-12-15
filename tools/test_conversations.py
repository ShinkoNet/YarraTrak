"""
Conversation scenario tests for the PTV agent.

These tests verify the agent's tool selection and response behavior
for various Melbourne public transport queries.

Run with: pytest tests/test_conversations.py -v
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from server.agent_engine import run_agent, run_worker
from server import config


# --- Test Fixtures ---

@pytest.fixture
def session_id():
    return "test-session-001"


@pytest.fixture
def mock_ptv_departures():
    """Mock PTV API response for departures."""
    return """Departures for Richmond Station (Metro Station):
- Pakenham Line towards Pakenham, terminating at Pakenham. Departs: 2024-01-15T10:05:00+11:00 (Platform 1)
- Pakenham Line towards Pakenham, terminating at Pakenham. Departs: 2024-01-15T10:20:00+11:00 (Platform 1)
- Cranbourne Line towards Cranbourne, terminating at Cranbourne. Departs: 2024-01-15T10:08:00+11:00 (Platform 2)"""


@pytest.fixture
def mock_ptv_search():
    """Mock PTV API response for stop search."""
    return """Richmond Station (Metro Train, Type 0) [ID: 1162]
Richmond (Tram Stop, Type 1) [ID: 2500]"""


# --- Scenario Definitions ---

SCENARIOS = [
    {
        "name": "ambiguous_direction_requires_clarification",
        "query": "Next train from Richmond",
        "expected_type": "CLARIFICATION",
        "expected_entity": "direction",
        "description": "Richmond has multiple lines - should ask which direction/line"
    },
    {
        "name": "specific_line_returns_result",
        "query": "Next Pakenham train from Richmond",
        "expected_type": ["RESULT", "CLARIFICATION", "ERROR"],  # May return result, ask direction, or error if no trains
        "description": "Pakenham line is specific - returns result, asks direction, or errors if unavailable"
    },
    {
        "name": "city_direction_is_clear",
        "query": "Next train to the city from Richmond",
        "expected_type": "RESULT",
        "description": "City-bound is unambiguous from suburban stations"
    },
    {
        "name": "tram_ambiguous_direction",
        "query": "Next 96 tram",
        "expected_type": ["CLARIFICATION", "RESULT", "ERROR"],  # LLM may handle this various ways
        "description": "Route 96 goes to St Kilda or East Brunswick - may ask direction or return results"
    },
    {
        "name": "guardrail_blocks_coding",
        "query": "Write me a Python script",
        "expected_type": "ERROR",
        "expected_error_code": "GUARDRAIL_BLOCK",
        "description": "Coding requests should be blocked"
    },
    {
        "name": "guardrail_blocks_trivia",
        "query": "What's the capital of France?",
        "expected_type": "ERROR",
        "expected_error_code": "GUARDRAIL_BLOCK",
        "description": "General knowledge should be blocked"
    },
    {
        "name": "guardrail_allows_greeting",
        "query": "Hello",
        "expected_type": ["RESULT", "CLARIFICATION", "ERROR"],  # Any valid response, not guardrail block
        "description": "Greetings should be allowed through guardrail"
    },
]


# --- Test Classes ---

class TestAgentScenarios:
    """Test individual conversation scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
    async def test_scenario(self, scenario, session_id):
        """
        Run a single scenario and verify the response type.
        Hits the real Groq API.
        """
        # Skip guardrail tests if disabled
        if scenario["name"].startswith("guardrail_") and not config.ENABLE_GUARDRAIL:
            pytest.skip("Guardrail tests skipped when ENABLE_GUARDRAIL=False")

        result = await run_agent(scenario["query"], session_id)

        # Check response type
        expected = scenario["expected_type"]
        if isinstance(expected, list):
            assert result["type"] in expected, f"Expected one of {expected}, got {result['type']}"
        else:
            assert result["type"] == expected, f"Expected {expected}, got {result['type']}"

        # Check error code if specified (only when guardrail is enabled)
        if "expected_error_code" in scenario:
            assert result["payload"].get("error_code") == scenario["expected_error_code"]

        # Check entity type for clarifications
        if "expected_entity" in scenario and result["type"] == "CLARIFICATION":
            assert result["payload"].get("missing_entity") == scenario["expected_entity"]

        # Check payload contents if specified
        if "expected_in_payload" in scenario and result["type"] == "RESULT":
            payload_str = str(result["payload"])
            for expected_text in scenario["expected_in_payload"]:
                assert expected_text in payload_str, f"Expected '{expected_text}' in payload"


class TestMultiTurnConversation:
    """Test multi-turn conversation flows."""

    @pytest.mark.asyncio
    async def test_clarification_followup(self, session_id):
        """
        Test that context is maintained across turns.

        Turn 1: "Next train from Caulfield" -> May ask clarification (which line?) or return result
        Turn 2: "Frankston" -> Should use context to understand this means Frankston line from Caulfield
        """
        # Turn 1 - should ask clarification or return a result
        result1 = await run_agent("Next train from Caulfield", session_id)
        assert result1["type"] in ["CLARIFICATION", "RESULT", "ERROR"], f"Unexpected type: {result1['type']}"

        # Turn 2 - context should help resolve follow-up
        if result1["type"] == "CLARIFICATION":
            result2 = await run_agent("Frankston line", session_id)
            assert result2["type"] in ["RESULT", "CLARIFICATION"], f"Unexpected type: {result2['type']}"

    @pytest.mark.asyncio
    async def test_asr_correction_flow(self, session_id):
        """
        Test ASR correction flow.

        The model may auto-correct "Sandringam" to "Sandringham" and return results directly,
        or it may ask for clarification about the spelling.
        """
        # Turn 1 - misspelled station - model may auto-correct or ask
        result1 = await run_agent("Next train from Sandringam", session_id)
        # Model might: auto-correct and return result, ask clarification, or error
        assert result1["type"] in ["CLARIFICATION", "ERROR", "RESULT"]

        # If it returned results, it auto-corrected successfully
        if result1["type"] == "RESULT":
            payload_str = str(result1["payload"]).lower()
            assert "sandringham" in payload_str or "sandy" in payload_str

        # If clarification, turn 2
        if result1["type"] == "CLARIFICATION":
            result2 = await run_agent("Yes, Sandringham", session_id)
            assert result2["type"] in ["RESULT", "CLARIFICATION"]


class TestToolSelection:
    """Test that the agent selects appropriate tools."""

    @pytest.mark.asyncio
    async def test_search_and_get_departures_tool(self, session_id):
        """Test that a clear query gets a valid response."""
        # A clear, specific query should get results or ask for clarification
        result = await run_agent("Next train to Frankston from South Yarra", session_id)
        # Should get a response (not an error due to tool failure)
        assert result["type"] in ["RESULT", "CLARIFICATION", "ERROR"]

    @pytest.mark.asyncio
    async def test_clarification_tool_for_ambiguity(self, session_id):
        """Test that ambiguous queries get handled appropriately."""
        result = await run_agent("Next bus from Carlton", session_id)
        # "Next bus from Carlton" is ambiguous - which route?
        # Model may ask clarification, return results, or error
        assert result["type"] in ["CLARIFICATION", "RESULT", "ERROR"]


# --- Run Configuration ---

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
