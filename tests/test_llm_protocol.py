"""Exercise outgoing DeepSeek payloads without credentials or external calls."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from autogen_core import FunctionCall
from autogen_core.models import AssistantMessage, FunctionExecutionResult, FunctionExecutionResultMessage, UserMessage


class ModelProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        spec = importlib.util.spec_from_file_location("llm_protocol_test", Path(__file__).resolve().parents[1] / "workflow_agents/common/llm.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-only-key", "DEEPSEEK_MODEL": "deepseek-flash",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "DEEPSEEK_CHAT_MODEL": "retired-chat", "DEEPSEEK_REASONER_MODEL": "retired-reasoner",
            "QWEN_MODEL": "retired-vision", "QWEN_API_KEY": "test-retired-key",
        }):
            spec.loader.exec_module(self.module)
        self.client = self.module.deepseek_flash
        await self.client.close()
        self.requests = []
        self.client._client = AsyncOpenAI(
            api_key="test-only-key", base_url="https://api.deepseek.com", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)),
        )

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        # Exercise the actual outgoing payload for JSON, tools and streaming.
        for parameter in ('max_tokens', 'max_completion_tokens', 'max_output_tokens'):
            self.assertNotIn(parameter, body)
        response = {"id": "probe", "object": "chat.completion", "created": 1,
                    "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        if body.get("stream"):
            chunk = {"id": "probe", "object": "chat.completion.chunk", "created": 1,
                     "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "stop",
                     "delta": {"content": "OK"}}]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=response)

    async def asyncTearDown(self):
        await self.client.close()

    def test_legacy_roles_cannot_select_different_models(self):
        for retired in ('deepseekV3', 'deepseekR1', 'qwen'):
            self.assertFalse(hasattr(self.module, retired))
        self.assertEqual(self.module.deepseek_model, "deepseek-flash")

    async def test_json_without_tools_keeps_high_reasoning(self):
        result = await self.client.create([UserMessage(content="Return JSON.", source="user")], json_output=True)
        self.assertTrue(json.loads(result.content)["ok"])
        body = self.requests[0]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["response_format"]["type"], "json_object")

    async def test_tool_followup_disables_thinking_without_changing_model(self):
        messages = [UserMessage(content="Read tool result.", source="user"),
                    AssistantMessage(content=[FunctionCall(id="probe_call", name="probe", arguments="{}")], source="assistant"),
                    FunctionExecutionResultMessage(content=[FunctionExecutionResult(
                        call_id="probe_call", name="probe", content="314159")])]
        tool = {"name": "probe", "description": "Probe", "parameters": {"type": "object", "properties": {}}}
        await self.client.create(messages, tools=[tool], extra_create_args={"reasoning_effort": "high"})
        body = self.requests[0]
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["messages"][-1]["tool_call_id"], "probe_call")
        # A later request without tools must not inherit the tool-mode override.
        await self.client.create([UserMessage(content="Return JSON.", source="user")])
        self.assertEqual(self.requests[1]["reasoning_effort"], "high")

    async def test_streaming_tools_also_use_compatible_mode(self):
        tool = {"name": "probe", "description": "Probe", "parameters": {"type": "object", "properties": {}}}
        chunks = [chunk async for chunk in self.client.create_stream(
            [UserMessage(content="Reply OK.", source="user")], tools=[tool])]
        self.assertIn("OK", chunks)
        self.assertTrue(self.requests[0]["stream"])
        self.assertEqual(self.requests[0]["model"], "deepseek-flash")
        self.assertEqual(self.requests[0]["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()
