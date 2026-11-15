from dataclasses import replace
import pytest
from app.agent import ChatRequest, AgentError, run_agent

class FakeProvider:
    def __init__(self, messages): self.messages = iter(messages)
    async def complete(self, *args): return next(self.messages), {}, 'stop'

def call(name, args='{}'):
    import json
    return {'tool_calls': [{'id': name, 'type': 'function', 'function': {'name': name, 'arguments': args}}]}

@pytest.mark.asyncio
@pytest.mark.parametrize('question,tool,required', [
    ('Какие регионы имеют максимальную долю DPD 90+?', 'get_top_risk_segments', 'Сравнение регионов'),
    ('Сравни риск просрочки по регионам', 'analyze_segment', 'Сравнение регионов'),
    ('Как распределён остаток по корзинам просрочки?', 'get_delinquency_distribution', 'DPD 90+'),
    ('Сравни продукты по доле просроченного остатка', 'analyze_segment', 'Сравнение продуктов'),
])
async def test_evidence_renderer(settings, question, tool, required):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    import json
    args = {'dimension': 'region'} if tool == 'analyze_segment' and 'регион' in question else {'dimension':'product'} if tool == 'analyze_segment' else {}
    result = await run_agent(ChatRequest(question=question), cfg, FakeProvider([call(tool, json.dumps(args)), {'content': 'Нет источника'}]))
    assert required in result['answer']
    assert '[T2]' in result['answer']
    assert result['trace'][1]['tool'] == tool

@pytest.mark.asyncio
async def test_wrong_tool_not_counted_as_distribution(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    with pytest.raises(AgentError):
        await run_agent(ChatRequest(question='Покажи доли задолженности по DPD-бакетам'), cfg,
                        FakeProvider([call('get_portfolio_summary'), {'content':'Обзор [T2]'}, {'content':'Обзор [T2]'}]))

@pytest.mark.asyncio
async def test_driver_evidence(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    import json
    args={'start_date':'2026-08-31','end_date':'2026-09-30','dimension':'product'}
    result = await run_agent(ChatRequest(question='Разложи изменение NPL 90 по продуктам на структурный и внутрисегментный вклад'), cfg,
                             FakeProvider([call('explain_npl_change', json.dumps(args)), {'content':'Нет источника'}]))
    assert 'внутрисегментный' in result['answer']
    assert 'арифметическая атрибуция' in result['answer']
