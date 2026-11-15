from dataclasses import replace
import pytest
from app.agent import ChatRequest, AgentError, run_agent

class FakeProvider:
    def __init__(self, messages):
        self.messages = iter(messages)
    async def complete(self, *args):
        return next(self.messages), {}, 'stop'

@pytest.mark.asyncio
async def test_quality_fallback(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    call = {'tool_calls': [{'id': 'q1', 'type': 'function', 'function': {'name': 'check_data_quality', 'arguments': '{}'}}]}
    result = await run_agent(ChatRequest(question='Проверь качество данных и целостность'), cfg,
                             FakeProvider([call, {'content': 'Ответ без ссылки'}]))
    assert 'Источник MCP: [T2]' in result['answer']
    assert result['trace'][1]['tool'] == 'check_data_quality' and result['trace'][1]['ok']

@pytest.mark.asyncio
async def test_overview_fallback(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    call = {'tool_calls': [{'id': 's1', 'type': 'function', 'function': {'name': 'get_portfolio_summary', 'arguments': '{}'}}]}
    result = await run_agent(ChatRequest(question='Каков общий объём задолженности и процент просрочки 90+ сейчас?'), cfg,
                             FakeProvider([call, {'content': 'Ответ без ссылки'}]))
    assert 'Источник MCP: [T2]' in result['answer']
    assert result['trace'][1]['tool'] == 'get_portfolio_summary' and result['trace'][1]['ok']

@pytest.mark.asyncio
async def test_error_retains_trace(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    with pytest.raises(AgentError) as err:
        await run_agent(ChatRequest(question='Неподтверждённый ответ'), cfg,
                        FakeProvider([{'content': 'Без ссылок'}, {'content': 'Снова без ссылок'}]))
    assert err.value.mcp_trace[0]['tool'] == 'get_dataset_context'
    assert err.value.mcp_trace[0]['ok']
