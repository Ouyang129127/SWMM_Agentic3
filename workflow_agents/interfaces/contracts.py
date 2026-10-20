from typing import Any
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    flow_version: int | None = None

class ChatResponse(BaseModel):
    session_id: str
    reply: str
    transcript: list[dict[str, str]]
    workflow: dict[str, Any] = Field(default_factory=dict)
