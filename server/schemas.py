"""
Pydantic schemas for strict JSON validation of agent tool responses.

These schemas ensure type-safe, validated responses from the agent's terminal tools.
All schemas use strict validation with no additional properties allowed.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from enum import Enum


# --- Enums ---

class RouteTypeEnum(str, Enum):
    TRAIN = "TRAIN"
    TRAM = "TRAM"
    BUS = "BUS"
    VLINE = "VLINE"
    NIGHT_BUS = "NIGHT_BUS"


class MissingEntityType(str, Enum):
    DIRECTION = "direction"
    LINE = "line"
    STOP = "stop"


# --- Terminal Tool Schemas ---

class Departure(BaseModel):
    """Single departure entry."""
    time: str = Field(..., description="Departure time (HH:MM)")
    platform: Optional[str] = Field(None, description="Platform number or null")
    minutes_to_depart: int = Field(..., ge=0, description="Minutes until departure")

    model_config = {"extra": "forbid"}

    @field_validator("platform", mode="before")
    @classmethod
    def coerce_platform(cls, v):
        """Coerce platform to string (model sometimes passes int)."""
        if v is not None:
            return str(v)
        return v


class ReturnResultPayload(BaseModel):
    """Schema for return_result tool response."""
    destination: str = Field(..., description="Where the service is heading")
    line: str = Field(..., description="Line/route name (e.g., 'Pakenham Line', 'Route 96')")
    departure: Departure = Field(..., description="The next departure")
    tts_text: str = Field(..., description="Natural speech for TTS")

    model_config = {"extra": "forbid"}


class ClarificationOption(BaseModel):
    """Single option for ask_clarification."""
    label: str = Field(..., description="Display text")
    value: str = Field(..., description="Value to use if selected")

    model_config = {"extra": "forbid"}


class AskClarificationPayload(BaseModel):
    """Schema for ask_clarification tool response."""
    question_text: str = Field(..., description="Question to ask the user")
    missing_entity: MissingEntityType = Field(..., description="What's missing: 'direction', 'line', 'stop'")
    options: list[ClarificationOption] = Field(..., min_length=2, max_length=6, description="Options to present")

    model_config = {"extra": "forbid"}


class ReturnErrorPayload(BaseModel):
    """Schema for return_error tool response."""
    message: str = Field(..., description="Error message for the user")
    tts_text: str = Field(..., description="Spoken error message")

    model_config = {"extra": "forbid"}


# --- Data Tool Parameter Schemas ---

class SearchAndGetDeparturesParams(BaseModel):
    """Parameters for search_and_get_departures tool."""
    query: str = Field(..., description="Stop name (e.g., 'Richmond', 'Flinders Street')")
    route_type: RouteTypeEnum = Field(default=RouteTypeEnum.TRAIN, description="Transport mode")

    model_config = {"extra": "forbid"}


class SearchStopsParams(BaseModel):
    """Parameters for search_stops tool."""
    query: str = Field(..., description="Stop name to search")

    model_config = {"extra": "forbid"}


class SearchRoutesParams(BaseModel):
    """Parameters for search_routes tool."""
    query: str = Field(..., description="Route name to search")

    model_config = {"extra": "forbid"}


class GetDeparturesParams(BaseModel):
    """Parameters for get_departures tool."""
    stop_id: int = Field(..., description="PTV stop ID")
    route_type: RouteTypeEnum = Field(default=RouteTypeEnum.TRAIN, description="Transport mode")

    model_config = {"extra": "forbid"}


class GetRouteDirectionsParams(BaseModel):
    """Parameters for get_route_directions tool."""
    route_id: int = Field(..., description="PTV route ID")

    model_config = {"extra": "forbid"}


# --- JSON Schema Generation ---

def get_strict_json_schema(model: type[BaseModel]) -> dict:
    """Generate strict JSON schema from Pydantic model with additionalProperties: false."""
    schema = model.model_json_schema()
    return _make_strict(schema)


def _make_strict(schema: dict) -> dict:
    """Recursively add additionalProperties: false to all objects."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        
        # Process all values
        for key, value in schema.items():
            if key == "$defs":
                # Process definitions
                for def_name, def_schema in value.items():
                    _make_strict(def_schema)
            elif isinstance(value, dict):
                _make_strict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _make_strict(item)
    
    return schema


# --- Pre-generated strict schemas for tool definitions ---

RETURN_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "destination": {"type": "string", "description": "Where the service is heading"},
        "line": {"type": "string", "description": "Line/route name (e.g., 'Pakenham Line', 'Route 96')"},
        "departure": {
            "type": "object",
            "properties": {
                "time": {"type": "string", "description": "Departure time (HH:MM)"},
                "platform": {"type": ["string", "integer", "null"], "description": "Platform number or null"},
                "minutes_to_depart": {"type": "integer", "minimum": 0, "description": "Minutes until departure"}
            },
            "required": ["time", "minutes_to_depart"],
            "additionalProperties": False,
            "description": "The next departure"
        },
        "tts_text": {"type": "string", "description": "Natural speech for TTS (e.g., 'Next train in 5 minutes from platform 3')"}
    },
    "required": ["destination", "line", "departure", "tts_text"],
    "additionalProperties": False
}

ASK_CLARIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string", "description": "Question to ask (e.g., 'Which direction?')"},
        "missing_entity": {
            "type": "string",
            "enum": ["direction", "line", "stop"],
            "description": "What's missing: 'direction', 'line', 'stop'"
        },
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Display text"},
                    "value": {"type": "string", "description": "Value to use if selected"}
                },
                "required": ["label", "value"],
                "additionalProperties": False
            },
            "minItems": 2,
            "maxItems": 6
        }
    },
    "required": ["question_text", "missing_entity", "options"],
    "additionalProperties": False
}

RETURN_ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "Error message for the user"},
        "tts_text": {"type": "string", "description": "Spoken error message"}
    },
    "required": ["message", "tts_text"],
    "additionalProperties": False
}

# Data tool schemas
SEARCH_AND_GET_DEPARTURES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Stop name (e.g., 'Richmond', 'Flinders Street')"},
        "route_type": {
            "type": "string",
            "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
            "default": "TRAIN",
            "description": "Transport mode"
        }
    },
    "required": ["query"],
    "additionalProperties": False
}

SEARCH_STOPS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Stop name to search"}
    },
    "required": ["query"],
    "additionalProperties": False
}

SEARCH_ROUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Route name to search"}
    },
    "required": ["query"],
    "additionalProperties": False
}

GET_DEPARTURES_SCHEMA = {
    "type": "object",
    "properties": {
        "stop_id": {"type": "integer", "description": "PTV stop ID"},
        "route_type": {
            "type": "string",
            "enum": ["TRAIN", "TRAM", "BUS", "VLINE", "NIGHT_BUS"],
            "default": "TRAIN",
            "description": "Transport mode"
        }
    },
    "required": ["stop_id"],
    "additionalProperties": False
}

GET_ROUTE_DIRECTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "route_id": {"type": "integer", "description": "PTV route ID"}
    },
    "required": ["route_id"],
    "additionalProperties": False
}

SETUP_PEBBLE_BUTTON_SCHEMA = {
    "type": "object",
    "properties": {
        "button_id": {"type": "integer", "enum": [1, 2, 3], "description": "Button number (1, 2, or 3)"},
        "start_station": {"type": "string", "description": "Name of START station (e.g., 'Narre Warren', 'Richmond')"},
        "destination": {"type": "string", "description": "Name of DESTINATION station (e.g., 'Flinders Street', 'the city')"},
        "route_type": {
            "type": "string",
            "enum": ["TRAIN", "TRAM", "VLINE"],
            "default": "TRAIN",
            "description": "Transport mode"
        }
    },
    "required": ["button_id", "start_station", "destination"],
    "additionalProperties": False
}


# --- Validation helpers ---

def validate_return_result(payload: dict) -> ReturnResultPayload:
    """Validate and parse return_result payload."""
    return ReturnResultPayload.model_validate(payload)


def validate_ask_clarification(payload: dict) -> AskClarificationPayload:
    """Validate and parse ask_clarification payload."""
    return AskClarificationPayload.model_validate(payload)


def validate_return_error(payload: dict) -> ReturnErrorPayload:
    """Validate and parse return_error payload."""
    return ReturnErrorPayload.model_validate(payload)
