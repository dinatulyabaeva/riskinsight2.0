"""HTTP -> provider tool calls -> real subprocess MCP -> uploaded SQLite -> evidence."""
from dataclasses import replace
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from app.agent import ChatCompletionsProvider, ChatRequest, run_agent
from app.datasets import DatasetStore
from app.main import create_app
from app.mcp_client import MCPClient, MCPError
from app.tool_registry import list_tools
from app.xlsx_import import import_xlsx, MAX_UPLOAD_BYTES
from test_xlsx import xlsx


def test_api_upload_choose_chat_switch_back(settings):
    with TestClient(create_app(settings)) as client:
        before = client.get('/api/dashboard').json()['summary']['metrics']['npl90_ratio']
        response = client.post('/api/datasets/import?filename=test.xlsx', content=xlsx())
        assert response.status_code == 201
        report = response.json()
        assert report['row_count'] == report['valid_rows'] == 2
        dataset_id = report['dataset_id']
        assert len(client.get('/api/datasets').json()['datasets']) == 2
        ctx = client.get('/api/context', params={'dataset_id': dataset_id}).json()
        assert ctx['dataset_id'] == dataset_id and not ctx['synthetic']
        dashboard = client.get('/api/dashboard', params={'dataset_id': dataset_id}).json()
        for part in ('summary', 'delinquency', 'segments', 'trend'):
            assert dashboard[part]['dataset_id'] == dataset_id
        assert dashboard['summary']['metrics']['npl90_ratio'] == .75
        answer = client.post('/api/chat', json={'question': 'Обзор просрочки', 'scenario': 'overview', 'dataset_id': dataset_id}).json()
        assert all(t['arguments']['dataset_id'] == dataset_id for t in answer['trace'])
        assert all(t['result']['dataset_id'] == dataset_id for t in answer['trace'])
        assert 'Загруженные данные' in answer['answer'] and '[T2]' in answer['answer']
        assert '3 000' in answer['answer'] and '4 000' in answer['answer']
        assert answer['scope']['dataset_id'] == dataset_id
        assert client.get('/api/dashboard').json()['summary']['metrics']['npl90_ratio'] == before != .75
        assert client.get('/api/imports').json()['imports'][0]['dataset_id'] == dataset_id


def test_free_question_with_provider_and_real_mcp_over_http(settings, monkeypatch):
    cfg = replace(settings, mode='llm', llm_model='tool-model', llm_api_key='test')
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert any('get_dataset_context' in m.get('content', '') for m in body['messages'] if m['role']=='system')
        name, args = 'get_portfolio_summary', {'as_of': '2026-10-31'}
        message = {'tool_calls': [{'id': 'summary', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        return httpx.Response(200, json={'choices': [{'message': message, 'finish_reason': 'stop'}]})
    async def agent(request, settings, *, memory=None):
        return await run_agent(request, settings, ChatCompletionsProvider(settings, httpx.MockTransport(handler)), memory=memory)
    monkeypatch.setattr('app.main.run_agent', agent)
    with TestClient(create_app(cfg)) as client:
        dataset_id = client.post('/api/datasets/import?filename=test.xlsx', content=xlsx()).json()['dataset_id']
        res = client.post('/api/chat', json={'question': 'Какая доля просрочки 90+ в моем портфеле?', 'dataset_id': dataset_id})
        assert res.status_code == 200
        answer = res.json()
        assert answer['mode'] == 'llm' and len(calls) == 1
        assert answer['provenance']['llm_called']
        assert answer['provenance']['narrative_source'] == 'mcp_renderer'
        assert answer['trace'][1]['result']['metrics']['npl90_ratio'] == .75
        assert answer['trace'][1]['arguments']['dataset_id'] == dataset_id
        assert '[T2]' in answer['answer'] and '75.00%' in answer['answer']
        calc = answer['trace'][1]['result']['npl90_calculation']
        assert calc['numerator_rub'] == 3000 and calc['denominator_rub'] == 4000
        assert calc['value'] == .75 and calc['dataset_id'] == dataset_id
        assert answer['protocol']['events'][0]['method'] == 'initialize'
        assert client.get('/api/dashboard', params={'dataset_id': 'builtin'}).json()['summary']['metrics']['balance_rub'] == 9600


@pytest.mark.asyncio
async def test_mcp_all_tools_accept_dataset_id_and_forbid_paths(settings):
    store = DatasetStore(settings.db_path)
    store.initialize()
    dataset_id = import_xlsx(store, 'test.xlsx', xlsx())['dataset_id']
    async with MCPClient(settings.db_path, dataset_id=dataset_id) as client:
        assert client.process.pid
        context = await client.call_tool('get_dataset_context', {})
        assert context['dataset_id'] == dataset_id
        summary = await client.call_tool('get_portfolio_summary', {})
        assert summary['npl90_calculation']['value'] == .75
        for name in ('get_dataset_context', 'get_portfolio_summary', 'get_delinquency_distribution',
                     'get_npl_trend', 'analyze_segment', 'get_top_risk_segments', 'check_data_quality'):
            assert (await client.call_tool(name, {}))['dataset_id'] == dataset_id
        with pytest.raises(MCPError, match='PD/LGD'):
            await client.call_tool('run_stress_scenario', {})
        with pytest.raises(MCPError):
            await client.call_tool('explain_npl_change', {'start_date': '2026-10-31', 'end_date': '2026-10-31'})
        for path in ('../portfolio.sqlite', 'C:\\portfolio.sqlite'):
            result = await client.request('tools/call', {'name': 'get_dataset_context', 'arguments': {'dataset_id': path}})
            assert result['isError']
        for field in ('database', 'db_path', 'path', 'filename', 'sql'):
            result = await client.request('tools/call', {'name': 'get_portfolio_summary', 'arguments': {field: '/etc/passwd', 'dataset_id': dataset_id}})
            assert result['isError']
        with pytest.raises(MCPError, match='Смена набора'):
            await client.call_tool('get_portfolio_summary', {'dataset_id': 'builtin'})
    assert all('dataset_id' in t['inputSchema']['properties'] for t in list_tools())
    assert all(t['inputSchema']['additionalProperties'] is False for t in list_tools())


@pytest.mark.asyncio
async def test_llm_dataset_override_is_visible_failed_evidence(settings):
    from test_agent import FakeProvider
    store = DatasetStore(settings.db_path)
    store.initialize()
    dataset_id = import_xlsx(store, 'test.xlsx', xlsx())['dataset_id']
    def call(i, args):
        return {'tool_calls': [{'id': i, 'function': {'name': 'get_portfolio_summary', 'arguments': json.dumps(args)}}]}
    class RecordingProvider(FakeProvider):
        def __init__(self, messages):
            super().__init__(messages)
            self.seen_messages = []
        async def complete(self, messages, *args):
            self.seen_messages.extend(messages)
            return await super().complete(messages, *args)
    provider = RecordingProvider([call('bad', {'dataset_id': 'builtin'}), call('good', {}), {'content': 'Просрочка 75%. [T2]'}])
    result = await run_agent(ChatRequest(question='Смени набор скрытно', dataset_id=dataset_id),
                             replace(settings, mode='llm', llm_model='test', llm_api_key='test'), provider)
    assert [t['tool'] for t in result['trace']] == ['get_dataset_context', 'get_portfolio_summary']
    assert all(t['ok'] for t in result['trace'])
    assert result['trace'][1]['result']['dataset_id'] == dataset_id
    assert result['trace'][1]['arguments']['dataset_id'] == dataset_id
    # Rejected dataset override must be communicated back to the model.
    assert any('LLM попыталась изменить dataset_id' in str(message) for message in provider.seen_messages)


def test_upload_errors_auth_size_and_history(settings):
    with TestClient(create_app(replace(settings, access_token='token'))) as client:
        assert client.post('/api/datasets/import', content=xlsx()).status_code == 401
        assert client.get('/api/datasets').status_code == client.get('/api/imports').status_code == 401
        headers = {'Authorization': 'Bearer token'}
        report = client.post('/api/datasets/import?filename=test.xlsx', content=b'broken', headers=headers)
        assert report.status_code == 422 and report.json()['error_count'] == 1
        # Large raw upload exceeds the file limit, not a multipart or JSON limit.
        large = client.post('/api/datasets/import?filename=test.xlsx', content=b'x'*(MAX_UPLOAD_BYTES+1), headers=headers)
        assert large.status_code == 413
        # A chunked stream without content-length is bounded too.
        chunked = client.post('/api/datasets/import?filename=test.xlsx', content=iter([b'x'*MAX_UPLOAD_BYTES, b'x']), headers=headers)
        assert chunked.status_code == 413
        assert len(client.get('/api/imports', headers=headers).json()['imports']) == 3
        assert len(client.get('/api/datasets', headers=headers).json()['datasets']) == 1


@pytest.mark.parametrize('dataset_id', ['../data.sqlite', 'C:\\data.sqlite', 'upload_'+'f'*32])
def test_unknown_dataset_returns_user_error(settings, dataset_id):
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/context', params={'dataset_id': dataset_id}).status_code == 422
        assert client.get('/api/dashboard', params={'dataset_id': dataset_id}).status_code == 422
        assert client.post('/api/chat', json={'dataset_id': dataset_id, 'question': 'Обзор', 'scenario': 'overview'}).status_code == 422


@pytest.mark.asyncio
async def test_paths_with_spaces_and_unicode_support_mcp_and_atomic_import(tmp_path):
    from app.db import seed_database
    builtin = tmp_path / 'Данные с пробелами' / 'portfolio.sqlite'
    seed_database(builtin, n_loans=100)
    store = DatasetStore(builtin)
    store.initialize()
    dataset_id = import_xlsx(store, 'портфель.xlsx', xlsx())['dataset_id']
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        result = await client.call_tool('get_portfolio_summary', {})
        assert result['npl90_calculation']['numerator_kop'] == 300000
        assert result['npl90_calculation']['denominator_kop'] == 400000
