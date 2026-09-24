"""OpenHands tool `start_demo(command, port)`: ask the controller to serve the demo.

This module runs inside the Agent Server, in the sandbox. The agent image copies it
on its own (with the package `__init__`), so it imports only the standard library,
pydantic and the OpenHands SDK. The Agent Server loads it with
`--import-modules dgx_autonomy.demo_tool`.

The tool starts no process itself. It writes a request into the agent's workspace
and waits for the answer. The trusted controller validates the request, records the
demo spec, and runs the command in the sandbox in a separate session (the `demo`
process group). That session is not part of agent execution, so it survives when
the deadline or `dgx-autonomy stop` ends the agent, and the operator can still open
the app afterwards.

    agent -> /workspace/.dgx/demo-request.json   {id, command, port}
    controller -> /dgx-control/demo.json          {request_id, state, message, ...}
                  (read-only for the agent)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Self

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from pydantic import Field

if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState

# The file protocol with the controller (controller.py keeps the host side).
REQUEST_PATH = "/workspace/.dgx/demo-request.json"
STATUS_PATH = "/dgx-control/demo.json"
DEMO_LOG_PATH = "/workspace/.dgx/demo.log"
PROJECT_DIR = "/workspace/project"
DEMO_PORT = 3000
FINAL_STATES = frozenset({"running", "failed", "refused"})
WAIT_S = 240.0

DESCRIPTION = f"""Serve the app so the operator can open it in a browser.

* The environment runs `command` for you with `sh -c` in {PROJECT_DIR},
  in a separate process group that keeps running after your work ends. A process you
  start yourself in the terminal (for example with `&` or `nohup`) is stopped when
  your work ends, so the operator would never see it.
* The command must stay in the foreground and serve on port {DEMO_PORT}, host 0.0.0.0.
  PORT={DEMO_PORT} and HOST=0.0.0.0 are set for it. Port {DEMO_PORT} is the only port
  the operator can reach, and it belongs to the demo: whatever else listens on it is
  ended when the demo starts. Test your own servers on other ports.
  Examples: `pnpm start`, `npx serve -l {DEMO_PORT} dist`,
  `python3 -m http.server {DEMO_PORT} --bind 0.0.0.0`.
* Build first; the command should start quickly. Calling the tool again replaces the
  running demo.
* The command's output goes to {DEMO_LOG_PATH}.
* The tool waits until the port is listening, the command exits, or the environment
  refuses the request, and reports which.
"""


class StartDemoAction(Action):
    command: str = Field(
        description=(
            f"Shell command that serves the app in the foreground on port {DEMO_PORT} "
            "and host 0.0.0.0, run from the project directory."
        )
    )
    port: int = Field(
        default=DEMO_PORT,
        description=f"Port the command listens on. Must be {DEMO_PORT}.",
    )


class StartDemoObservation(Observation):
    """What the controller answered."""


def _write_request(path: str, body: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{body['id']}.tmp"
    with open(tmp, "w") as f:
        json.dump(body, f)
    os.replace(tmp, path)


def _read_status(path: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class StartDemoExecutor(ToolExecutor[StartDemoAction, StartDemoObservation]):
    def __init__(
        self,
        request_path: str = REQUEST_PATH,
        status_path: str = STATUS_PATH,
        wait_s: float = WAIT_S,
        poll_s: float = 1.0,
    ) -> None:
        self.request_path = request_path
        self.status_path = status_path
        self.wait_s = wait_s
        self.poll_s = poll_s

    def __call__(
        self,
        action: StartDemoAction,
        conversation: LocalConversation | None = None,
    ) -> StartDemoObservation:
        request_id = uuid.uuid4().hex
        _write_request(
            self.request_path,
            {"id": request_id, "command": action.command, "port": action.port},
        )
        end = time.monotonic() + self.wait_s
        status: dict[str, Any] | None = None
        while time.monotonic() < end:
            status = _read_status(self.status_path)
            if (
                status
                and status.get("request_id") == request_id
                and status.get("state") in FINAL_STATES
            ):
                break
            time.sleep(self.poll_s)
        return _observation(request_id, status)


def _observation(request_id: str, status: dict[str, Any] | None) -> StartDemoObservation:
    if not status or status.get("request_id") != request_id:
        return StartDemoObservation.from_text(
            "The environment has not picked up the demo request yet. Wait a little and "
            "call start_demo again if the app is still not served.",
            is_error=True,
        )
    state = str(status.get("state"))
    message = str(status.get("message") or "")
    if state == "running":
        return StartDemoObservation.from_text(
            f"The demo is running and listening on port {status.get('port', DEMO_PORT)}. "
            f"{message}".strip()
        )
    if state == "starting":
        return StartDemoObservation.from_text(
            f"The demo is still starting (not listening yet). Its output is in "
            f"{DEMO_LOG_PATH}. {message}".strip(),
            is_error=True,
        )
    return StartDemoObservation.from_text(f"The demo was {state}: {message}".strip(), is_error=True)


class StartDemoTool(ToolDefinition[StartDemoAction, StartDemoObservation]):
    @classmethod
    def create(cls, conv_state: ConversationState | None = None, **params: Any) -> Sequence[Self]:
        return [
            cls(
                description=DESCRIPTION,
                action_type=StartDemoAction,
                observation_type=StartDemoObservation,
                annotations=ToolAnnotations(
                    title="start_demo",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=StartDemoExecutor(),
            )
        ]


register_tool(StartDemoTool.name, StartDemoTool)
