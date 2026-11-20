"""Test-only application: network LLM double + production agent/MCP/SQLite.

Never used by app.main:app. Makes browser acceptance reproducible without Ollama or API charges.
"""
from dataclasses import replace
import json
import httpx
import app.main as main
from app.agent import ChatCompletionsProvider, run_agent
from app.config import Settings


def handler(request):
    # Only the network planner is replaced. Context, selected analytics,
    # grounded report, chart generation and conversation memory remain real.
    body = json.loads(request.content)
    assert any(t['function']['name'] == 'get_portfolio_summary' for t in body['tools'])
    tool_call = {'id': 'browser-summary', 'type': 'function',
                 'function': {'name': 'get_portfolio_summary', 'arguments': '{}'}}
    return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [tool_call]}, 'finish_reason': 'tool_calls'}]})


async def test_agent(request, settings, *, memory=None):
    return await run_agent(request, settings, ChatCompletionsProvider(settings, httpx.MockTransport(handler)), memory=memory)


main.run_agent = test_agent
settings = replace(Settings.from_env(), mode='llm', llm_model='browser-test-double',
                   llm_api_key='not-a-real-key', seed_loans=100, requests_per_minute=100)
app = main.create_app(settings)
