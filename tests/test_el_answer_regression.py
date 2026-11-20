"""LLM selects the real MCP summary; its numeric report preserves EL/null."""
from dataclasses import replace
from pathlib import Path
import pytest
from app.agent import ChatRequest, run_agent
from app.datasets import DatasetStore
from app.xlsx_import import import_xlsx


class SelectingProvider:
    def __init__(self):
        self.calls = 0

    async def complete(self, *args):
        self.calls += 1
        return {"content": "EL равен 99999999 — этот текст нельзя публиковать", "tool_calls": [
            {"id": "summary", "type": "function", "function": {
                "name": "get_portfolio_summary", "arguments": "{}"}}]}, {}, "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename, expected_value, expected_text", [
    ("portfolio-full.xlsx", 290.0, "290.00"),
    ("portfolio.xlsx", None, "не рассчитан"),
])
async def test_el_from_real_mcp_after_model_selection(settings, filename, expected_value, expected_text):
    store = DatasetStore(settings.db_path)
    store.initialize()
    path = Path(__file__).resolve().parents[1] / "examples" / filename
    report = import_xlsx(store, filename, path.read_bytes())
    assert report["status"] == "imported"
    request = ChatRequest(dataset_id=report["dataset_id"],
                          question="Каковы ожидаемые потери EL загруженного кредитного портфеля на последнюю дату? Укажи сумму в рублях и источник MCP.")
    provider = SelectingProvider()
    result = await run_agent(request, replace(settings, mode="llm", llm_model="controlled-test", llm_api_key="test"), provider)
    assert provider.calls == result["usage"]["llm_requests"] == 1
    assert result["trace"][1]["result"]["metrics"]["el_rub"] == expected_value
    assert expected_text in result["answer"] and "[T2]" in result["answer"]
    assert "99999999" not in result["answer"]
    assert result["provenance"]["planner"] == "llm"
    assert result["provenance"]["narrative_source"] == "mcp_renderer"
    assert [entry["tool"] for entry in result["trace"]] == ["get_dataset_context", "get_portfolio_summary"]
