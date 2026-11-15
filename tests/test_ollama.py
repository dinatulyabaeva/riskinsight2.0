from dataclasses import replace
import json
import httpx
import pytest
from app.agent import OllamaProvider, AgentError, ChatRequest, run_agent


@pytest.mark.asyncio
async def test_native_ollama_full_loop_with_actual_mcp(settings):
    cfg = replace(settings, mode='llm', llm_api_style='ollama', llm_model='qwen3:8b',
                  llm_base_url='http://127.0.0.1:11434/v1', llm_api_key='')
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == 'http://127.0.0.1:11434/api/chat'
        assert body['think'] is False and body['stream'] is False
        assert body['options']['num_predict'] == cfg.max_output_tokens
        assert 'Authorization' not in request.headers
        if len(requests) == 1:
            message = {'content': '', 'tool_calls': [{'function': {'name': 'get_dataset_context', 'arguments': {}}}]}
        elif len(requests) == 2:
            assert body['messages'][-1]['tool_name'] == 'get_dataset_context'
            assert json.loads(body['messages'][-1]['content'])['dataset_id'] == 'builtin'
            message = {'content': '', 'tool_calls': [{'function': {'name': 'get_portfolio_summary', 'arguments': {}}}]}
        else:
            assert body['messages'][-1]['tool_name'] == 'get_portfolio_summary'
            assert json.loads(body['messages'][-1]['content'])['metrics']['balance_rub'] == 9600
            previous = [m for m in body['messages'] if m.get('tool_calls')]
            assert isinstance(previous[-1]['tool_calls'][0]['function']['arguments'], dict)
            message = {'content': 'Остаток 9600 RUB, DPD90+ 5800/9600. [T2]'}
        return httpx.Response(200, json={'done': True, 'done_reason': 'stop', 'message': message,
                                       'prompt_eval_count': 10, 'eval_count': 5})
    result = await run_agent(ChatRequest(question='Проверь просрочку'), cfg,
                             OllamaProvider(cfg, httpx.MockTransport(handler)))
    assert result['mode'] == 'llm' and len(requests) == 3
    assert all(t['ok'] for t in result['trace'])
    assert result['usage']['input_tokens'] == 30
    assert result['trace'][1]['result']['npl90_calculation']['numerator_rub'] == 5800


@pytest.mark.asyncio
async def test_final_round_omits_tools(settings):
    def handler(request):
        assert 'tools' not in json.loads(request.content)
        return httpx.Response(200, json={'done': True, 'message': {'content': 'Result'}})
    provider = OllamaProvider(settings, httpx.MockTransport(handler))
    message, _, _ = await provider.complete([], [{'invalid': True}], 'none')
    assert message['content'] == 'Result'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'done': True, 'error': 'secret'},
    {'done': True, 'message': {'tool_calls': [{'function': {'name': 'tool', 'arguments': 'not-object'}}]}}])
async def test_native_malformed_response_is_visible(settings, payload):
    provider = OllamaProvider(settings, httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    with pytest.raises(AgentError):
        await provider.complete([], [], 'auto')


@pytest.mark.asyncio
async def test_native_timeout_is_not_demo(settings):
    def handler(request):
        raise httpx.ReadTimeout('private data', request=request)
    with pytest.raises(AgentError, match='native /api/chat') as exc:
        await OllamaProvider(settings, httpx.MockTransport(handler)).complete([], [], 'auto')
    assert 'private data' not in str(exc.value)


def test_compact_schema_keeps_validation_and_property_names():
    from app.agent import compact_ollama_schema
    schema = {'title': 'Outer', 'type': 'object', 'additionalProperties': False,
              'properties': {'title': {'type': 'string', 'minLength': 1, 'description': 'Help'},
                             'dimension': {'enum': ['product', 'region'], 'default': 'product'}},
              'required': ['title']}
    compact = compact_ollama_schema(schema)
    assert 'title' not in compact
    assert compact['properties']['title'] == {'type': 'string', 'minLength': 1}
    assert compact['properties']['dimension']['enum'] == ['product', 'region']
    assert compact['additionalProperties'] is False and compact['required'] == ['title']


@pytest.mark.asyncio
async def test_native_first_round_is_compact_but_later_tools_available(settings):
    from app.agent import SYSTEM_PROMPT, OLLAMA_SYSTEM_PROMPT
    from app.tool_registry import list_tools
    tools = [{'type': 'function', 'function': {'name': t['name'], 'description': t['description'],
              'parameters': t['inputSchema']}} for t in list_tools()]
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'done': True, 'message': {'content': 'Test'}})
    provider = OllamaProvider(settings, httpx.MockTransport(handler))
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': 'Question'}]
    await provider.complete(messages, tools, 'required')
    await provider.complete(messages, tools, 'auto')
    assert [t['function']['name'] for t in captured[0]['tools']] == ['get_dataset_context']
    assert len(captured[1]['tools']) == 9
    assert captured[0]['messages'][0]['content'] == OLLAMA_SYSTEM_PROMPT
    assert messages[0]['content'] == SYSTEM_PROMPT
    baseline = len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False))
    assert len(json.dumps(captured[0], ensure_ascii=False)) < baseline * .5
