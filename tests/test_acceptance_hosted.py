"""HTTP eval transport/auth/continuity checks; no external model is called."""
from argparse import Namespace
import hashlib
import json

import httpx
import pytest

from app.evidence import build_report
from app.mcp_client import MCPClient, EvidenceSession
from scripts import eval_acceptance
from scripts.teaching_dataset import DATES, ROOT
from test_teaching_dataset import teaching_import


def arguments(tmp_path, **kwargs):
    return Namespace(provider="live", base_url="https://example.test", repeat=1, limit=None,
                     expected_commit="a" * 40, max_llm_requests=40, min_interval_seconds=0,
                     output=tmp_path / "hosted.json", **kwargs)


def health():
    return {"status": "ok", "mode": "llm", "llm_configured": True, "version": "2.1.0",
            "build_commit": "a" * 40,
            "features": {"teaching_dataset": True, "evidence_charts": True, "conversation_memory": True}}


@pytest.mark.asyncio
async def test_hosted_eval_uses_server_model_and_preserves_conversation_without_local_llm_key(teaching_import, tmp_path, monkeypatch):
    builtin, dataset_id = teaching_import
    chain = eval_acceptance.cases()[-3:]
    monkeypatch.setattr(eval_acceptance, "cases", lambda: chain)
    monkeypatch.setenv("APP_ACCESS_TOKEN", "private-token-for-hosted-eval")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # A hosted check must not use even otherwise-invalid local LLM settings.
    monkeypatch.setattr(eval_acceptance.Settings, "from_env", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("Local settings must not be read"))))
    expected_responses = []
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        for case in chain:
            session = EvidenceSession(client)
            await session.call("get_dataset_context")
            await session.call(case.tool, case.arguments)
            answer, charts, scope = build_report(session.trace, dataset_id)
            expected_responses.append({"answer": answer, "trace": session.trace, "visualizations": charts,
                                       "resolved_scope": scope, "conversation_id": "b" * 32,
                                       "usage": {"llm_requests": 1}, "provenance": {
                                           "llm_called": True, "planner": "llm", "numeric_source": "mcp",
                                           "model": "server-model", "narrative_source": "mcp_renderer",
                                           "model_selected_tools": [case.tool]}})
    workbook_hash = hashlib.sha256((ROOT / "examples/portfolio-scenarios.xlsx").read_bytes()).hexdigest()
    chats = []
    def handler(request):
        assert request.headers["authorization"] == "Bearer private-token-for-hosted-eval"
        if request.url.path == "/api/health":
            return httpx.Response(200, json=health())
        if request.url.path == "/api/datasets/example":
            return httpx.Response(200, json={"dataset_id": dataset_id, "source_sha256": workbook_hash})
        if request.url.path == "/api/context":
            return httpx.Response(200, json={"source_sha256": workbook_hash, "dates": list(DATES), "mode": "llm", "model": "server-model"})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert "memory" not in body and "history" not in body
        if chats:
            assert body["conversation_id"] == "b" * 32
        else:
            assert "conversation_id" not in body
        chats.append(body)
        return httpx.Response(200, json=expected_responses[len(chats) - 1])
    args = arguments(tmp_path)
    report = await eval_acceptance.evaluate_hosted(args, transport=httpx.MockTransport(handler))
    assert report["summary"]["passed_all_criteria"] == 3, report
    assert report["model"] == "server-model"
    assert report["deployment_unchanged"]
    assert report["summary"]["external_model_calls"] == 3  # Reported by mock server, not an external test run.
    assert len({r["client_request_id"] for r in chats}) == 3
    assert "private-token-for-hosted-eval" not in args.output.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_usage", [{}, {"llm_requests": 2}])
async def test_hosted_eval_saves_http_failure_and_partial_trace_without_retry(tmp_path, monkeypatch, error_usage):
    selected = eval_acceptance.cases()[:1]
    monkeypatch.setattr(eval_acceptance, "cases", lambda: selected)
    workbook_hash = hashlib.sha256((ROOT / "examples/portfolio-scenarios.xlsx").read_bytes()).hexdigest()
    chat_count = 0
    def handler(request):
        nonlocal chat_count
        if request.url.path == "/api/health":
            return httpx.Response(200, json=health())
        if request.url.path == "/api/datasets/example":
            return httpx.Response(200, json={"dataset_id": "upload_" + "c" * 32, "source_sha256": workbook_hash})
        if request.url.path == "/api/context":
            return httpx.Response(200, json={"source_sha256": workbook_hash, "dates": list(DATES), "mode": "llm", "model": "server-model"})
        chat_count += 1
        return httpx.Response(429, json={"detail": "Provider rate limit", "usage": error_usage,
                                        "trace": [{"ref": "T1", "tool": "get_dataset_context", "ok": True}]})
    report = await eval_acceptance.evaluate_hosted(arguments(tmp_path), transport=httpx.MockTransport(handler))
    assert chat_count == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["external_model_calls"] == error_usage.get("llm_requests")
    assert report["summary"]["requests_with_unknown_model_call_count"] == (0 if error_usage else 1)
    assert report["results"][0]["http_status"] == 429
    assert report["results"][0]["partial_trace"][0]["tool"] == "get_dataset_context"


@pytest.mark.asyncio
async def test_hosted_eval_rejects_wrong_release_before_any_chat(tmp_path):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={**health(), "build_commit": "d" * 40})
    with pytest.raises(ValueError, match="build_commit"):
        await eval_acceptance.evaluate_hosted(arguments(tmp_path), transport=httpx.MockTransport(handler))
    assert requests == ["/api/health"]


@pytest.mark.parametrize("url", ["http://example.test", "https://user:password@example.test", "https://example.test?token=secret"])
def test_hosted_url_does_not_accept_credentials_or_insecure_remote_transport(url):
    with pytest.raises(ValueError):
        eval_acceptance.validate_base_url(url)
