"""OpenHands tools `write_handoff` and `declare_blocked`: the builder talks to the controller.

Like demo_tool.py, this module runs inside the Agent Server in the sandbox and imports
only the standard library, pydantic and the OpenHands SDK. The Agent Server loads it
with `--import-modules`.

write_handoff answers a handoff request (the controller is about to replace the
conversation with a fresh one). declare_blocked says that no viable path is left.
Neither tool decides anything: each writes a request into the workspace and waits for
the controller's answer, which says whether it was accepted and, if not, why. The
agent can then fix the problems and call the tool again.

    agent -> /workspace/.dgx/handoff.json      {id, handoff...}
    controller -> /dgx-control/handoff.json    {request_id, accepted, problems, message}
    agent -> /workspace/.dgx/blocked.json      {id, missing_capability, ...}
    controller -> /dgx-control/blocked.json    {request_id, accepted, problems, message}
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Self

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState

HANDOFF_REQUEST_PATH = "/workspace/.dgx/handoff.json"
HANDOFF_STATUS_PATH = "/dgx-control/handoff.json"
BLOCKED_REQUEST_PATH = "/workspace/.dgx/blocked.json"
BLOCKED_STATUS_PATH = "/dgx-control/blocked.json"
WAIT_S = 120.0

HANDOFF_DESCRIPTION = """Hand your work over to the fresh conversation that replaces this one.

Call it only when a message titled HANDOFF REQUEST asks you to, with the request_id
from that message. Everything the next conversation will know comes from this
handoff, the brief, and the project's files. Evidence is a path of a file in the
project (relative to /workspace/project) or eval:N for acceptance evaluation N. An
item marked done must name evidence. Report what you did, not what you hoped: the
environment's verified results are recorded separately. The tool answers whether the
handoff was accepted; if not, fix the problems it lists and call it again.
"""

BLOCKED_DESCRIPTION = """Declare that the brief cannot be completed with what you have.

Use it only when no viable path is left: name the concrete capability or permission
that is missing (not a bug you have not fixed yet), at least two different approaches
you tried, and what would be needed to proceed. A first declaration is reviewed by a
fresh conversation before the run ends; the environment does not extend the deadline
or grant permissions. Operating restrictions (no API keys, no payments, no access to
other services) are boundaries, not blockers to work around.
"""


class RoadmapItem(BaseModel):
    item: str = Field(description="A piece of the planned work.")
    status: Literal["done", "in_progress", "todo", "blocked"]
    evidence: list[str] = Field(
        default_factory=list,
        description="Project file paths (relative to /workspace/project) or eval:N.",
    )


class Decision(BaseModel):
    decision: str
    why: str = ""


class Attempt(BaseModel):
    approach: str
    outcome: Literal["worked", "failed", "abandoned"]
    notes: str = ""


class OpenFailure(BaseModel):
    what: str
    detail: str = ""


class WriteHandoffAction(Action):
    request_id: str = Field(description="The id from the HANDOFF REQUEST message.")
    summary: str = Field(description="Where the work stands, in a short paragraph.")
    roadmap: list[RoadmapItem] = Field(description="The whole plan, item by item, with status.")
    decisions: list[Decision] = Field(default_factory=list)
    attempts: list[Attempt] = Field(
        default_factory=list, description="Approaches tried, including the ones that failed."
    )
    open_failures: list[OpenFailure] = Field(default_factory=list)
    next_steps: list[str] = Field(description="What the next conversation should do first.")
    notes: str = ""


class DeclareBlockedAction(Action):
    missing_capability: str = Field(
        description="The concrete capability or permission that is missing."
    )
    alternatives_tried: list[str] = Field(
        description="At least two different approaches you tried, and how each failed."
    )
    needed: str = Field(description="What would be needed to proceed.")


class ControllerAnswer(Observation):
    """What the controller answered."""


def _write(path: str, body: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{body['id']}.tmp"
    with open(tmp, "w") as f:
        json.dump(body, f)
    os.replace(tmp, path)


def _read(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _ask(request_path: str, status_path: str, body: dict[str, Any], wait_s: float,
         poll_s: float) -> ControllerAnswer:  # fmt: skip
    request_id = uuid.uuid4().hex
    _write(request_path, {"id": request_id, **body})
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        status = _read(status_path)
        if status and status.get("request_id") == request_id:
            message = str(status.get("message") or "")
            problems = [str(p) for p in status.get("problems") or []]
            if status.get("accepted"):
                return ControllerAnswer.from_text(message or "Accepted.")
            text = message or "Not accepted."
            if problems:
                text += "\nProblems:\n" + "\n".join(f"- {p}" for p in problems)
            return ControllerAnswer.from_text(text, is_error=True)
        time.sleep(poll_s)
    return ControllerAnswer.from_text(
        "The environment has not answered yet. Call the tool again in a moment.", is_error=True
    )


class WriteHandoffExecutor(ToolExecutor[WriteHandoffAction, ControllerAnswer]):
    def __init__(
        self,
        request_path: str = HANDOFF_REQUEST_PATH,
        status_path: str = HANDOFF_STATUS_PATH,
        wait_s: float = WAIT_S,
        poll_s: float = 1.0,
    ) -> None:
        self.paths = (request_path, status_path)
        self.wait_s = wait_s
        self.poll_s = poll_s

    def __call__(
        self, action: WriteHandoffAction, conversation: LocalConversation | None = None
    ) -> ControllerAnswer:
        body = action.model_dump(exclude={"kind"}, mode="json")
        return _ask(*self.paths, body, self.wait_s, self.poll_s)


class DeclareBlockedExecutor(ToolExecutor[DeclareBlockedAction, ControllerAnswer]):
    def __init__(
        self,
        request_path: str = BLOCKED_REQUEST_PATH,
        status_path: str = BLOCKED_STATUS_PATH,
        wait_s: float = WAIT_S,
        poll_s: float = 1.0,
    ) -> None:
        self.paths = (request_path, status_path)
        self.wait_s = wait_s
        self.poll_s = poll_s

    def __call__(
        self, action: DeclareBlockedAction, conversation: LocalConversation | None = None
    ) -> ControllerAnswer:
        body = action.model_dump(exclude={"kind"}, mode="json")
        return _ask(*self.paths, body, self.wait_s, self.poll_s)


class WriteHandoffTool(ToolDefinition[WriteHandoffAction, ControllerAnswer]):
    @classmethod
    def create(cls, conv_state: ConversationState | None = None, **params: Any) -> Sequence[Self]:
        return [
            cls(
                description=HANDOFF_DESCRIPTION,
                action_type=WriteHandoffAction,
                observation_type=ControllerAnswer,
                annotations=ToolAnnotations(
                    title="write_handoff",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=WriteHandoffExecutor(),
            )
        ]


class DeclareBlockedTool(ToolDefinition[DeclareBlockedAction, ControllerAnswer]):
    @classmethod
    def create(cls, conv_state: ConversationState | None = None, **params: Any) -> Sequence[Self]:
        return [
            cls(
                description=BLOCKED_DESCRIPTION,
                action_type=DeclareBlockedAction,
                observation_type=ControllerAnswer,
                annotations=ToolAnnotations(
                    title="declare_blocked",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DeclareBlockedExecutor(),
            )
        ]


register_tool(WriteHandoffTool.name, WriteHandoffTool)
register_tool(DeclareBlockedTool.name, DeclareBlockedTool)
