"""A raw OpenAI-style tool call against the owned llama-server.

This is the compatibility gate the research left open: the GGUF's embedded template,
llama.cpp's tool-call parser and the OpenAI response shape that LiteLLM (inside
OpenHands) consumes. It goes through the controller's restricted inference
passthrough because the server is only reachable on the private network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from dgx_helpers import wait_for

pytestmark = pytest.mark.dgx

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}


@pytest.fixture(scope="module")
def inference(control: Callable[..., Any]) -> dict[str, Any]:
    control("inference.ensure")
    return wait_for(
        lambda: control("inference.status"),
        lambda s: s["ready"] or s["container"] not in ("running",),
        timeout_s=30 * 60,
    )


def _chat(control: Callable[..., Any], body: dict[str, Any]) -> dict[str, Any]:
    res = control(
        "inference.request", {"method": "POST", "path": "/v1/chat/completions", "body": body}
    )
    assert res["status"] == 200, res
    return dict(res["body"])


def test_inference_is_ready(inference: dict[str, Any]) -> None:
    assert inference["ready"], inference
    assert inference["model_key"] == "qwen3.6-35b-a3b"
    assert inference["slots_total"] and inference["slots_total"] >= 1


def test_model_emits_a_parseable_tool_call(
    control: Callable[..., Any], inference: dict[str, Any]
) -> None:
    body = _chat(
        control,
        {
            "model": inference["model_key"],
            "messages": [
                {
                    "role": "user",
                    "content": "What is the weather in Halifax right now? Use the tool.",
                }
            ],
            "tools": [WEATHER_TOOL],
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 2048,
        },
    )
    message = body["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    assert calls, f"no tool call; message was {message}"
    call = calls[0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    args = json.loads(call["function"]["arguments"])
    assert "halifax" in args["city"].lower()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["usage"]["prompt_tokens"] > 0


def test_tool_result_round_trip(control: Callable[..., Any], inference: dict[str, Any]) -> None:
    body = _chat(
        control,
        {
            "model": inference["model_key"],
            "messages": [
                {"role": "user", "content": "What is the weather in Halifax? Use the tool."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Halifax"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"temp_c": 14, "sky": "fog"}',
                },
            ],
            "tools": [WEATHER_TOOL],
            "temperature": 0,
            "max_tokens": 2048,
        },
    )
    message = body["choices"][0]["message"]
    assert not message.get("tool_calls"), message
    assert "14" in (message.get("content") or ""), message
