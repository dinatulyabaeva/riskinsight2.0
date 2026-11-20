from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.agent import ChatRequest, AgentError
from app.conversations import ConversationConflict, ConversationStore
from app.main import create_app


def request(**kwargs):
    return ChatRequest(question="Покажи тренд просрочки", **kwargs)


def test_memory_is_bounded_private_and_copied():
    store = ConversationStore(max_turns=2)
    first, reset = store.resolve(request())
    assert not reset
    for i in range(3):
        store.remember(first, f"Вопрос {i}", {"answer": str(i), "resolved_scope": {"filters": {"region": "Центральный"}}})
    memory = first.memory()
    assert [t["answer"] for t in memory["turns"]] == ["1", "2"]
    memory["last_scope"]["filters"]["region"] = "Подмена"
    assert first.last_scope["filters"]["region"] == "Центральный"
    assert store.resolve(request(conversation_id=first.identifier))[0] is first
    second, reset = store.resolve(request(conversation_id=first.identifier, dataset_id="upload_" + "a" * 32))
    assert reset and second.identifier != first.identifier and not second.turns
    changed, reset = store.resolve(request(conversation_id=first.identifier, filters={"region": "Сибирский"}))
    assert reset and not changed.turns


def test_expiry_and_capacity_are_explicit():
    now = [0.0]
    store = ConversationStore(ttl=10, max_conversations=2, clock=lambda: now[0])
    first, _ = store.resolve(request())
    now[0] = 11
    second, reset = store.resolve(request(conversation_id=first.identifier))
    assert reset and first.identifier not in store.conversations
    store.resolve(request())
    store.resolve(request())
    assert len(store.conversations) == 2
    assert second.identifier not in store.conversations


def test_request_identity_does_not_reuse_another_question():
    store = ConversationStore()
    req = request(client_request_id="b" * 32)
    ticket = store.ticket(req)
    store.cache(ticket, {"answer": "Результат"})
    assert store.ticket(req).response["answer"] == "Результат"
    with pytest.raises(ConversationConflict):
        store.ticket(req.model_copy(update={"question": "Другой вопрос"}))


def _result():
    return {"answer": "Набор builtin. [T1]", "trace": [{"ref": "T1", "ok": True}],
            "mode": "llm", "duration_ms": 1, "resolved_scope": {
                "dataset_id": "builtin", "start_date": "2026-08-31", "end_date": "2026-09-30",
                "filters": {"region": "Центральный"}}}


def test_api_passes_only_server_memory_and_retries_do_not_duplicate(settings, monkeypatch):
    seen = []
    async def fake_run(body, cfg, *, memory):
        seen.append(memory)
        return _result()
    monkeypatch.setattr("app.main.run_agent", fake_run)
    cfg = replace(settings, mode="llm", llm_model="test", llm_api_key="test")
    with TestClient(create_app(cfg)) as client:
        payload = {"question": "Покажи тренд", "client_request_id": "1" * 32}
        first = client.post("/api/chat", json=payload)
        assert first.status_code == 200
        data = first.json()
        assert not seen[0]["turns"] and not data["conversation_reset"]
        retry = client.post("/api/chat", json=payload).json()
        assert retry["cache_hit"] and retry["request_id"] == data["request_id"] and len(seen) == 1
        follow = client.post("/api/chat", json={"question": "А теперь только по этому региону",
                    "conversation_id": data["conversation_id"], "client_request_id": "2" * 32})
        assert follow.status_code == 200
        assert len(seen[1]["turns"]) == 1
        assert seen[1]["last_scope"]["end_date"] == "2026-09-30"
        forged = client.post("/api/chat", json={"question": "Покажи тренд", "memory": {"answer": "Чужие числа"}})
        assert forged.status_code == 422


def test_api_failed_turn_is_not_remembered(settings, monkeypatch):
    failure = AgentError("Модель недоступна")
    failure.mcp_trace = [{"tool": "get_dataset_context", "ok": True, "ref": "T1"}]
    fake = AsyncMock(side_effect=[failure, _result()])
    monkeypatch.setattr("app.main.run_agent", fake)
    cfg = replace(settings, mode="llm", llm_model="test", llm_api_key="test")
    with TestClient(create_app(cfg)) as client:
        payload = {"question": "Покажи тренд", "client_request_id": "3" * 32}
        failed = client.post("/api/chat", json=payload)
        assert failed.status_code == 502 and failed.json()["request_id"]
        assert failed.json()["trace"][0]["tool"] == "get_dataset_context"
        assert client.post("/api/chat", json=payload).status_code == 200
        assert not fake.call_args.kwargs["memory"]["turns"]


def test_teaching_example_available_and_reused(settings):
    with TestClient(create_app(settings)) as client:
        download = client.get("/api/examples/portfolio-scenarios.xlsx")
        assert download.status_code == 200 and download.content[:2] == b"PK"
        first = client.post("/api/datasets/example").json()
        second = client.post("/api/datasets/example").json()
        assert first["status"] == "imported" and first["row_count"] == 14400
        assert second["reused"] and first["dataset_id"] == second["dataset_id"]
        context = client.get("/api/context", params={"dataset_id": first["dataset_id"]}).json()
        assert context["synthetic"] and len(context["dates"]) == 12
        assert context["source_sha256"] == first["source_sha256"]


def test_demo_error_is_validation_error_and_provider_retry_header_survives(settings, monkeypatch):
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/chat", json={"question": "Не выбран сценарий"})
        assert response.status_code == 422
    error = AgentError("Квота провайдера", status_code=429, retry_after=31)
    monkeypatch.setattr("app.main.run_agent", AsyncMock(side_effect=error))
    cfg = replace(settings, mode="llm", llm_model="test", llm_api_key="test")
    with TestClient(create_app(cfg)) as client:
        response = client.post("/api/chat", json={"question": "Покажи тренд"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "31"
