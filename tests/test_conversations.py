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
        "expected_type": "RESULT",
        "expected_in_payload": ["Pakenham"],
        "description": "Pakenham line is specific enough - should return result"
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
        "expected_type": "CLARIFICATION",
        "expected_entity": "direction",
        "description": "Route 96 goes to St Kilda or East Brunswick - need direction"
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

        Note: These tests hit the real Groq API. For CI, you may want to
        mock the API calls or use recorded responses.
        """
        pytest.skip("Requires GROQ_API_KEY - run manually with API key set")

        result = await run_agent(scenario["query"], session_id)

        # Check response type
        expected = scenario["expected_type"]
        if isinstance(expected, list):
            assert result["type"] in expected, f"Expected one of {expected}, got {result['type']}"
        else:
            assert result["type"] == expected, f"Expected {expected}, got {result['type']}"

        # Check error code if specified
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

        Turn 1: "Next train from Richmond" -> Clarification (which line?)
        Turn 2: "Pakenham" -> Should use context to understand this means Pakenham line from Richmond
        """
        pytest.skip("Requires GROQ_API_KEY - run manually with API key set")

        # Turn 1
        result1 = await run_agent("Next train from Richmond", session_id)
        assert result1["type"] == "CLARIFICATION"

        # Turn 2 - context should help resolve "Pakenham"
        result2 = await run_agent("Pakenham", session_id)
        assert result2["type"] == "RESULT"
        assert "Pakenham" in str(result2["payload"])

    @pytest.mark.asyncio
    async def test_asr_correction_flow(self, session_id):
        """
        Test ASR correction flow.

        Turn 1: "Next train from Packingham" (misheard) -> Should ask "Did you mean Pakenham?"
        Turn 2: "Yes" -> Should return Pakenham results
        """
        pytest.skip("Requires GROQ_API_KEY - run manually with API key set")

        # Turn 1 - misspelled station
        result1 = await run_agent("Next train from Packingham", session_id)
        # Could be CLARIFICATION asking if they meant Pakenham, or ERROR if not found
        assert result1["type"] in ["CLARIFICATION", "ERROR"]

        # If clarification, turn 2
        if result1["type"] == "CLARIFICATION":
            result2 = await run_agent("Yes", session_id)
            assert result2["type"] == "RESULT"


class TestToolSelection:
    """Test that the agent selects appropriate tools."""

    @pytest.mark.asyncio
    async def test_search_and_get_departures_tool(self, session_id, mock_ptv_departures):
        """Test that clear queries use the combined search_and_get_departures tool."""
        pytest.skip("Requires mocking setup")

        # This would need proper mocking of the tool handlers
        pass

    @pytest.mark.asyncio
    async def test_clarification_tool_for_ambiguity(self, session_id):
        """Test that ambiguous queries trigger ask_clarification tool."""
        pytest.skip("Requires GROQ_API_KEY - run manually with API key set")

        result = await run_agent("Next tram", session_id)
        # "Next tram" is ambiguous - which stop? which route?
        assert result["type"] == "CLARIFICATION"


class TestVibrationEncoding:
    """Test vibration pattern generation."""

    def test_vibration_zero_minutes(self):
        from server.api import calculate_vibration
        pattern = calculate_vibration(0)
        assert len(pattern) > 0
        # Special "arriving now" pattern

    def test_vibration_single_digit(self):
        from server.api import calculate_vibration
        pattern = calculate_vibration(5)
        # 5 ones = 5 x (150, 150) pairs
        assert len(pattern) == 10

    def test_vibration_tens(self):
        from server.api import calculate_vibration
        pattern = calculate_vibration(20)
        # 2 tens = 2 x (500, 300) pairs
        assert len(pattern) == 4

    def test_vibration_mixed(self):
        from server.api import calculate_vibration
        pattern = calculate_vibration(23)
        # 2 tens + 3 ones, with separator adjustment
        assert len(pattern) > 0

    def test_vibration_hour(self):
        from server.api import calculate_vibration
        pattern = calculate_vibration(65)
        # 1 hour + 5 minutes
        assert len(pattern) > 0
        # First pulse should be 1000ms (hour indicator)
        assert pattern[0] == 1000


# --- Run Configuration ---

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
