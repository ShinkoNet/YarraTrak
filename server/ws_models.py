"""Validate untrusted WebSocket data before it enters shared server state."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from .config import MAX_FAVOURITE_BUTTONS, MAX_QUERY_LENGTH

Identifier = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
Mode = Annotated[int, Field(strict=True, ge=0, le=4)]


class InputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Button(InputModel):
    button_id: Annotated[int, Field(ge=1, le=MAX_FAVOURITE_BUTTONS)]
    stop_id: Identifier
    route_type: Mode = 0
    direction_id: Identifier | None = None
    dest_id: Identifier | None = None


def validate_buttons(value):
    if not isinstance(value, list) or len(value) > MAX_FAVOURITE_BUTTONS:
        raise ValueError('Invalid favourite list')
    buttons = [Button.model_validate(b).model_dump() for b in value]
    if len({b['button_id'] for b in buttons}) != len(buttons):
        raise ValueError('Duplicate favourite slot')
    return buttons


class Message(InputModel):
    id: Annotated[str, Field(max_length=128)] | Identifier | None = None


class Ping(Message):
    type: Literal['ping', 'watch_stop']


class Subscribe(Message):
    type: Literal['subscribe_favourites']
    buttons: list[Button] = Field(default_factory=list, max_length=MAX_FAVOURITE_BUTTONS)


class Favourite(Message):
    type: Literal['favourite']
    stop_id: Identifier | None = None
    route_type: Mode = 0
    direction_id: Identifier | None = None
    dest_id: Identifier | None = None


class Watch(Message):
    type: Literal['watch_start']
    run_ref: Identifier
    stop_id: Identifier
    route_type: Mode = 0
    route_id: Identifier | None = None
    direction_id: Identifier | None = None

    @field_validator('run_ref', 'route_id', mode='before')
    @classmethod
    def wire_identifier(cls, value):
        # The phone's compact protocol represents these two fields as strings.
        if value == '':
            return None
        if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 10:
            return int(value)
        return value


class History(InputModel):
    stop_id: Identifier | None = None
    stop_name: str | None = Field(default=None, max_length=100)
    route_type: Mode | None = None
    text: str = Field(default='', max_length=160)
    at: Identifier | None = None


class Query(Message):
    type: Literal['query']
    text: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    session_id: str | None = Field(default=None, max_length=128)
    llm_api_key: str | None = Field(default=None, max_length=512)
    query_history: list[History] = Field(default_factory=list, max_length=5)
    current_entries: int | None = Field(default=None, ge=0, le=MAX_FAVOURITE_BUTTONS)


_message = TypeAdapter(Annotated[Ping | Subscribe | Favourite | Watch | Query, Field(discriminator='type')])


def validate_message(value):
    result = _message.validate_python(value).model_dump()
    if result['type'] == 'subscribe_favourites':
        result['buttons'] = validate_buttons(result['buttons'])
    if result['type'] == 'query':
        result['query_history'] = [h for h in result['query_history']
            if h['stop_id'] is not None and h['route_type'] is not None and h['stop_name']]
    return result
