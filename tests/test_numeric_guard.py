"""Regression tests for financial amounts backed by real MCP evidence."""
from dataclasses import replace
import pytest
from app.agent import ChatRequest, run_agent, _monetary_claims_supported


class FakeProvider:
    def __init__(self, messages):
        self.messages = iter(messages)
    async def complete(self, *args):
        return next(self.messages), {}, 'stop'


@pytest.mark.asyncio
async def test_stress_10x_wrong_but_cited_is_replaced_with_mcp(settings):
    cfg = replace(settings, mode='llm', llm_model='test', llm_api_key='test')
    call = {'tool_calls': [{'id': 's1', 'type': 'function', 'function': {
        'name': 'run_stress_scenario',
        'arguments': '{"pd_multiplier":1.5,"lgd_add_pp":5}'}}]}
    bogus = {'content': 'Стрессовый EL 68,7 млн руб., изменение 18,2 млн руб. [T2]'}
    result = await run_agent(ChatRequest(question='Стресс: PD ×1,5 LGD +5 п.п. EL'), cfg,
                             FakeProvider([call, bogus]))
    assert result['trace'][1]['tool'] == 'run_stress_scenario'
    assert result['trace'][1]['ok']
    data = result['trace'][1]['result']
    assert f"{data['stressed_el_rub']:,.2f}".replace(',', ' ') in result['answer']
    assert f"{data['delta_el_rub']:,.2f}".replace(',', ' ') in result['answer']
    assert '68,7 млн' not in result['answer']
    assert '[T2]' in result['answer']


def test_currency_guard_rejects_factor_of_ten():
    trace = [{'ok': True, 'tool': 'run_stress_scenario',
              'result': {'base_el_rub': 504773285.56, 'stressed_el_rub': 687228229.34,
                         'delta_el_rub': 182454943.78}}]
    assert not _monetary_claims_supported('Стрессовый EL 68,7 млн руб.', trace)
    assert _monetary_claims_supported('Стрессовый EL 687,23 млн руб.', trace)
    assert _monetary_claims_supported('Стрессовый EL 687 228 229,34 ₽.', trace)
