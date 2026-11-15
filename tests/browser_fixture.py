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
    body = json.loads(request.content)
    evidence = [json.loads(m['content']) for m in body['messages'] if m['role'] == 'tool']
    if not evidence:
        name, arguments = 'get_dataset_context', '{}'
    elif len(evidence) == 1:
        name, arguments = 'get_portfolio_summary', '{}'
    else:
        summary = evidence[-1]
        calc = summary['npl90_calculation']
        answer = (f"DPD 90+ = {calc['numerator_rub']} / {calc['denominator_rub']} RUB = {calc['value'] * 100:.2f}%. "
                  f"Формула: {calc['formula']}. Дата: {calc['as_of']}, набор: {calc['dataset_id']}. [T2]")
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}, 'finish_reason': 'stop'}]})
    tool_call = {'id': f'call{len(evidence)}', 'type': 'function', 'function': {'name': name, 'arguments': arguments}}
    return httpx.Response(200, json={'choices': [{'message': {'tool_calls': [tool_call]}, 'finish_reason': 'tool_calls'}]})


async def test_agent(request, settings):
    return await run_agent(request, settings, ChatCompletionsProvider(settings, httpx.MockTransport(handler)))


main.run_agent = test_agent
settings = replace(Settings.from_env(), mode='llm', llm_model='browser-test-double',
                   llm_api_key='not-a-real-key', seed_loans=100, requests_per_minute=100)
app = main.create_app(settings)
