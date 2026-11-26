"""A task owns one adapter instance and one execution-time policy boundary."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Decision(StrEnum):
    once = "approve_once"
    directory = "approve_session_dir"
    deny = "deny"


class FileContext(BaseModel):
    volume: str
    path: str = ""


class TaskRequest(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    prompt: str = Field(min_length=1, max_length=100_000)
    workspace: str = ""
    agent: str = "mock"
    context: FileContext | None = None
    desktop_handoff: bool = True


class Event(BaseModel):
    seq: int
    type: str
    data: dict[str, Any]


Emit = Callable[[str, dict], None]
Authorize = Callable[[str, list[str], bool], Awaitable[bool]]


class AgentAdapter(ABC):
    @abstractmethod
    async def run(self, task: TaskRequest, emit: Emit, authorize: Authorize) -> None:
        """Stream events and await authorize(command, paths, high_risk) before action."""

    @abstractmethod
    async def cancel(self) -> None:
        """Dispose resources, including the full spawned process tree."""

    def parse(self, event: dict) -> tuple[str, dict]:
        return event["type"], event["data"]

    def approval_response(self, pending, decision: Decision) -> None:
        if not pending.done():
            pending.set_result(decision)


REGISTRY: dict[str, type[AgentAdapter]] = {}


def register(name: str, adapter: type[AgentAdapter]) -> None:
    if name in REGISTRY and REGISTRY[name] is not adapter:
        raise ValueError(f"duplicate agent {name}")
    REGISTRY[name] = adapter
