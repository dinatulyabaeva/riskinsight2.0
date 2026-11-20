"""Responses wire-contract tests; model HTTP is mocked, MCP remains a real subprocess."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from app.agent import AgentError, ChatRequest, ResponsesProvider, run_agent
from app.config import Settings


FUNCTION = {
    "type": "function",
    "function": {
        "name": "get_portfolio_summary",
        "description": "Portfolio metrics from MCP",
        "parameters": {
            "type": "object",
            "properties": {"as_of": {"type": "string"}},
            "additionalProperties": False,
        },
        "strict": False,
    },
}


def configured(settings, **kwargs):
    defaults = dict(
        mode="llm", llm_api_style="responses",
        llm_base_url="https://ai.api.cloud.yandex.net/v1",
        llm_model="gpt://test-folder/aliceai-llm-flash",
        llm_api_key="private-test-key", llm_auth_scheme="api-key", llm_project="",
    )
    return replace(settings, **{**defaults, **kwargs})


def function_call(**kwargs):
    return {"type": "function_call", "id": "fc_item_id", "call_id": "call_pair_id",
            "name": "get_portfolio_summary", "arguments": "{}", "status": "completed", **kwargs}


def response(output, **kwargs):
    return {"id": "resp_example", "status": "completed", "output": output,
            "usage": {"input_tokens": 23, "output_tokens": 7}, **kwargs}


def text_item(text):
    return {"id": "msg_example", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["", "test-folder"])
async def test_yandex_flat_tools_auth_project_and_call_identity(settings, project):
    cfg = configured(settings, llm_project=project)
    messages = [{"role": "system", "content": "Select a tool."},
                {"role": "user", "content": "Обзор портфеля"}]
    original = deepcopy(messages)

    def handler(request):
        assert str(request.url) == "https://ai.api.cloud.yandex.net/v1/responses"
        assert request.headers["Authorization"] == "Api-Key private-test-key"
        assert request.headers["OpenAI-Project"] == "test-folder"
        body = json.loads(request.content)
        assert "messages" not in body and "previous_response_id" not in body
        assert body["input"] == messages
        assert body["stream"] is False and body["store"] is False
        assert body["max_output_tokens"] == cfg.max_output_tokens
        assert "max_completion_tokens" not in body
        assert body["tool_choice"] == "auto"
        assert body["tools"] == [{"type": "function", **FUNCTION["function"]}]
        return httpx.Response(200, json=response([function_call(arguments='{"as_of":"2026-09-30"}')]))

    message, usage, finish = await ResponsesProvider(cfg, httpx.MockTransport(handler)).complete(messages, [FUNCTION], "auto")
    assert messages == original
    assert message["tool_calls"] == [{"id": "call_pair_id", "type": "function",
                                      "function": {"name": "get_portfolio_summary", "arguments": '{"as_of":"2026-09-30"}'}}]
    assert usage == {"prompt_tokens": 23, "completion_tokens": 7}
    assert finish in {"stop", "tool_calls"}


@pytest.mark.asyncio
async def test_generic_responses_uses_bearer_without_yandex_project(settings):
    cfg = configured(settings, llm_base_url="https://provider.example/v1", llm_model="tool-model",
                     llm_auth_scheme="bearer")

    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert "OpenAI-Project" not in request.headers
        return httpx.Response(200, json=response([text_item("Done")], usage=None))

    message, usage, finish = await ResponsesProvider(cfg, httpx.MockTransport(handler)).complete([], [], "none")
    assert message["content"] == "Done" and finish == "stop"
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0}


@pytest.mark.asyncio
async def test_project_mismatch_fails_before_network(settings):
    def handler(request):
        pytest.fail("Mismatched Yandex folder must not reach provider")

    with pytest.raises(AgentError):
        await ResponsesProvider(configured(settings, llm_project="other-folder"),
                                httpx.MockTransport(handler)).complete([], [], "auto")


@pytest.mark.asyncio
async def test_tool_output_and_reasoning_replay_keep_call_id(settings):
    """The planner rebuilds assistant messages; transport must preserve response items."""
    cfg = configured(settings)
    reasoning = {"type": "reasoning", "id": "rs_private", "summary": [
        {"type": "summary_text", "text": "PRIVATE_REASONING_DO_NOT_RENDER"}]}
    outputs = [reasoning, function_call()]
    captured = []

    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(200, json=response(outputs))
        assert reasoning in body["input"]
        assert function_call() in body["input"]
        tool_outputs = [x for x in body["input"] if x.get("type") == "function_call_output"]
        assert tool_outputs == [{"type": "function_call_output", "call_id": "call_pair_id",
                                 "output": '{"source_ref":"T2","balance_rub":9600}'}]
        return httpx.Response(200, json=response([text_item("Ответ [T2]")], usage=None))

    provider = ResponsesProvider(cfg, httpx.MockTransport(handler))
    history = [{"role": "user", "content": "Обзор"}]
    first, _, _ = await provider.complete(history, [FUNCTION], "auto")
    assert "PRIVATE_REASONING_DO_NOT_RENDER" not in first.get("content", "")
    history.append({"role": "assistant", "content": first.get("content") or "", "tool_calls": first["tool_calls"]})
    history.append({"role": "tool", "tool_call_id": "call_pair_id",
                    "content": '{"source_ref":"T2","balance_rub":9600}'})
    final, _, _ = await provider.complete(history, [FUNCTION], "none")
    assert final["content"] == "Ответ [T2]"
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_length_limit_never_exposes_partial_tool_call(settings):
    payload = response([function_call(arguments='{"as_of":', status="incomplete")],
                       status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    provider = ResponsesProvider(configured(settings), httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    message, _, finish = await provider.complete([], [FUNCTION], "auto")
    assert finish == "length"
    assert not message.get("tool_calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    [function_call(arguments="not-json")],
    [function_call(arguments="[]")],
    [function_call(arguments={})],
    [function_call(call_id="")],
    [function_call(call_id=None)],
    [function_call(), function_call(id="another_item")],
    [function_call(status="incomplete")],
    [{"type": "web_search_call", "id": "web_call", "status": "completed"}],
    [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "PRIVATE_REFUSAL"}]}],
])
async def test_malformed_or_unavailable_output_is_not_executable(settings, output):
    provider = ResponsesProvider(configured(settings), httpx.MockTransport(lambda _: httpx.Response(200, json=response(output))))
    with pytest.raises(AgentError) as exc:
        await provider.complete([], [FUNCTION], "auto")
    assert "private-test-key" not in str(exc.value) and "PRIVATE_REFUSAL" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "in_progress", "cancelled", "failed"])
async def test_noncompleted_response_is_not_success(settings, status):
    payload = response([function_call()], status=status, error={"message": "PRIVATE_PROVIDER_MESSAGE"})
    provider = ResponsesProvider(configured(settings), httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(AgentError) as exc:
        await provider.complete([], [FUNCTION], "auto")
    assert "PRIVATE_PROVIDER_MESSAGE" not in str(exc.value)


@pytest.mark.asyncio
async def test_content_filter_is_not_length_or_success(settings):
    payload = response([], status="incomplete", incomplete_details={"reason": "content_filter"})
    provider = ResponsesProvider(configured(settings), httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    with pytest.raises(AgentError):
        await provider.complete([], [FUNCTION], "auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 503])
async def test_http_errors_keep_status_and_redact_provider_body(settings, status):
    def handler(request):
        return httpx.Response(status, headers={"Retry-After": "12"}, json={"error": "private-test-key PRIVATE_PROVIDER_MESSAGE"})

    provider = ResponsesProvider(configured(settings), httpx.MockTransport(handler))
    with pytest.raises(AgentError) as exc:
        await provider.complete([], [], "auto")
    assert exc.value.status_code == status
    assert str(status) in str(exc.value)
    assert "private-test-key" not in str(exc.value) and "PRIVATE_PROVIDER_MESSAGE" not in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 12


@pytest.mark.asyncio
async def test_timeout_is_visible_and_redacted(settings):
    def handler(request):
        raise httpx.ReadTimeout("PRIVATE_NETWORK_DETAIL", request=request)

    provider = ResponsesProvider(configured(settings), httpx.MockTransport(handler))
    with pytest.raises(AgentError) as exc:
        await provider.complete([], [], "auto")
    assert exc.value.status_code == 504
    assert "PRIVATE_NETWORK_DETAIL" not in str(exc.value)


@pytest.mark.asyncio
async def test_responses_factory_model_selection_and_real_mcp(settings, monkeypatch):
    """Only model HTTP is simulated: factory, MCP subprocess and numeric report run."""
    cfg = configured(settings)
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["tool_choice"] == "auto"
        assert all(t["name"] != "get_dataset_context" for t in body["tools"])
        assert any("get_dataset_context" in m.get("content", "") for m in body["input"] if m.get("role") == "system")
        return httpx.Response(200, json=response([function_call()]))

    monkeypatch.setattr("app.agent.ResponsesProvider", lambda actual_settings, transport=None: ResponsesProvider(actual_settings, httpx.MockTransport(handler)))
    result = await run_agent(ChatRequest(question="Обзор портфеля"), cfg)
    assert len(requests) == 1
    assert result["mode"] == "llm"
    assert [x["tool"] for x in result["trace"]] == ["get_dataset_context", "get_portfolio_summary"]
    assert all(x["ok"] for x in result["trace"])
    assert result["protocol"]["transport"] == "stdio"
    assert result["trace"][1]["result"]["metrics"]["balance_rub"] == 9600
    assert result["trace"][1]["result"]["npl90_calculation"]["numerator_rub"] == 5800
    assert "9 600.00" in result["answer"] and "[T2]" in result["answer"]
    assert result["provenance"]["llm_called"] is True
    assert result["provenance"]["planner"] == "llm"
    assert result["provenance"]["narrative_source"] == "mcp_renderer"
    assert result["provenance"]["model_selected_tools"] == ["get_portfolio_summary"]
    assert result["usage"]["input_tokens"] == 23
    assert "private-test-key" not in json.dumps(result)


def test_responses_settings_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_API_STYLE", "responses")
    monkeypatch.setenv("LLM_AUTH_SCHEME", "api-key")
    monkeypatch.setenv("LLM_PROJECT", "test-folder")
    monkeypatch.setenv("LLM_BASE_URL", "https://ai.api.cloud.yandex.net/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt://test-folder/aliceai-llm-flash")
    monkeypatch.setenv("LLM_API_KEY", "private-test-key")
    cfg = Settings.from_env()
    assert cfg.llm_api_style == "responses" and cfg.llm_configured
    assert cfg.llm_auth_scheme == "api-key" and cfg.llm_project == "test-folder"


def test_unknown_auth_scheme_rejected(monkeypatch):
    monkeypatch.setenv("LLM_AUTH_SCHEME", "unexpected")
    with pytest.raises(ValueError):
        Settings.from_env()
