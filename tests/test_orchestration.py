"""Contract tests: real MCP/SQLite; only model HTTP is controlled.

These tests do not measure a live model's understanding of paraphrases.
"""
from dataclasses import replace
import json
import pytest
from app.agent import AgentError, ChatRequest, run_agent
from app.db import seed_database
from app.evidence import build_report
from app.orchestration import explicit_dates, resolve_arguments
from app.tool_registry import list_tools


def call(name, arguments=None, content=""):
    return {"content": content, "tool_calls": [{"id": "call_" + name,
            "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)}}]}


class ModelDouble:
    def __init__(self, *messages):
        self.messages = iter(messages)
        self.calls = []

    async def complete(self, messages, schemas, choice):
        self.calls.append(json.loads(json.dumps(messages)))
        return next(self.messages), {"prompt_tokens": 100, "completion_tokens": 20}, "stop"


@pytest.fixture
def llm_settings(settings):
    return replace(settings, mode="llm", llm_model="controlled-test-provider", llm_api_key="test")


@pytest.fixture
def twelve_month_settings(settings, tmp_path):
    path = tmp_path / "twelve.sqlite"
    seed_database(path, n_loans=100)
    return replace(settings, db_path=path, mode="llm", llm_model="controlled-test-provider", llm_api_key="test")


@pytest.mark.asyncio
async def test_single_real_tool_selection_and_grounded_report(llm_settings):
    model = ModelDouble(call("get_portfolio_summary", content="Портфель 960000 миллиардов. [T999]"))
    result = await run_agent(ChatRequest(question="Какой размер портфеля?"), llm_settings, model)
    assert len(model.calls) == result["usage"]["llm_requests"] == 1
    assert result["trace"][1]["result"]["metrics"]["balance_rub"] == 9600
    assert "9 600.00" in result["answer"]
    assert "960000" not in result["answer"] and "T999" not in result["answer"]
    assert result["provenance"]["llm_called"] is True
    assert result["provenance"]["model_selected_tools"] == ["get_portfolio_summary"]
    assert result["provenance"]["narrative_source"] == "mcp_renderer"
    assert result["protocol"]["events"][0]["method"] == "initialize"


@pytest.mark.asyncio
@pytest.mark.parametrize("window,word", [(3, "три"), (6, "6"), (12, "двенадцать")])
async def test_exact_requested_window_and_chart_points(twelve_month_settings, window, word):
    result = await run_agent(ChatRequest(question=f"Покажи динамику DPD 90+ за последние {word} доступных месяцев"),
                             twelve_month_settings, ModelDouble(call("get_npl_trend")))
    evidence = result["trace"][1]
    points = evidence["result"]["points"]
    assert len(points) == window
    chart = result["visualizations"][0]
    assert chart["unit"] == "ratio" and chart["source_ref"] == evidence["ref"]
    assert chart["series"][0]["points"] == [{"label": p["as_of"], "value": p["npl90_ratio"]} for p in points]
    assert result["resolved_scope"]["start_date"] == points[0]["as_of"]
    assert result["resolved_scope"]["end_date"] == points[-1]["as_of"]


@pytest.mark.asyncio
async def test_wrong_window_corrected_before_execution(twelve_month_settings):
    model = ModelDouble(call("get_npl_trend", {"start_date": "2026-04-30", "end_date": "2026-09-30"}),
                        call("get_npl_trend", {"start_date": "2026-07-31", "end_date": "2026-09-30"}))
    result = await run_agent(ChatRequest(question="Динамика DPD 90+ за последние три месяца"), twelve_month_settings, model)
    assert len(model.calls) == 2
    assert len(result["trace"]) == 2  # rejected plan was never sent to MCP
    assert result["trace"][1]["arguments"]["start_date"] == "2026-07-31"
    assert "ожидается 2026-07-31" in json.dumps(result["selection_errors"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_followup_keeps_period_and_applies_explicit_product(twelve_month_settings):
    first = await run_agent(ChatRequest(question="Динамика DPD 90+ за последние три месяца"), twelve_month_settings, ModelDouble(call("get_npl_trend")))
    memory = {"last_scope": first["resolved_scope"], "turns": [{"question": "Динамика за три месяца", "answer": first["answer"]}]}
    model = ModelDouble(call("get_npl_trend"))
    result = await run_agent(ChatRequest(question="А теперь только потребительские, за тот же период"), twelve_month_settings, model, memory=memory)
    args = result["trace"][1]["arguments"]
    assert args["product"] == "Потребительский"
    assert args["start_date"] == "2026-07-31" and args["end_date"] == "2026-09-30"
    assert result["visualizations"][0]["filters"]["product"] == "Потребительский"
    assert "Память этого диалога" in json.dumps(model.calls[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_short_elliptical_followup_uses_server_memory(twelve_month_settings):
    memory = {"last_scope": {"dataset_id": "builtin", "as_of": "2026-09-30",
                            "start_date": "2026-07-31", "end_date": "2026-09-30", "filters": {}}}
    result = await run_agent(ChatRequest(question="Потребительские, пожалуйста"), twelve_month_settings,
                             ModelDouble(call("get_npl_trend")), memory=memory)
    assert result["trace"][1]["arguments"]["product"] == "Потребительский"
    assert result["trace"][1]["arguments"]["start_date"] == "2026-07-31"


@pytest.mark.asyncio
async def test_explicit_whole_portfolio_clears_inherited_filters(llm_settings):
    memory = {"last_scope": {"dataset_id": "builtin", "as_of": "2026-09-30", "filters": {"product": "Ипотека"}}}
    result = await run_agent(ChatRequest(question="Покажи весь портфель"), llm_settings, ModelDouble(call("get_portfolio_summary")), memory=memory)
    assert result["resolved_scope"]["filters"] == {}
    assert result["trace"][1]["result"]["metrics"]["balance_rub"] == 9600


@pytest.mark.asyncio
async def test_region_comparison_not_limited_to_previous_one_region(llm_settings):
    memory = {"last_scope": {"dataset_id": "builtin", "as_of": "2026-09-30", "filters": {"region": "Приволжский"}}}
    result = await run_agent(ChatRequest(question="Сравни регионы по просрочке"), llm_settings,
                             ModelDouble(call("analyze_segment", {"dimension": "region"})), memory=memory)
    assert "region" not in result["trace"][1]["arguments"]
    assert len(result["trace"][1]["result"]["segments"]) == 3


@pytest.mark.asyncio
async def test_memory_from_other_dataset_not_applied(llm_settings):
    memory = {"last_scope": {"dataset_id": "upload_" + "0" * 32, "filters": {"product": "Ипотека"}, "as_of": "2000-01-01"}}
    result = await run_agent(ChatRequest(question="А теперь общий остаток"), llm_settings, ModelDouble(call("get_portfolio_summary")), memory=memory)
    assert result["resolved_scope"]["filters"] == {}
    assert result["resolved_scope"]["as_of"] == "2026-09-30"


@pytest.mark.asyncio
async def test_model_cannot_add_unrequested_filter(llm_settings):
    model = ModelDouble(call("get_portfolio_summary", {"product": "Ипотека"}), call("get_portfolio_summary"))
    result = await run_agent(ChatRequest(question="Общий остаток портфеля"), llm_settings, model)
    assert len(model.calls) == 2
    assert result["trace"][1]["arguments"].get("product") is None
    assert result["trace"][1]["result"]["metrics"]["balance_rub"] == 9600


@pytest.mark.asyncio
async def test_known_missing_date_rejected_without_llm_cost(llm_settings):
    model = ModelDouble()
    with pytest.raises(AgentError, match="дата отсутствует") as err:
        await run_agent(ChatRequest(question="Дай остаток на 2030-01-31"), llm_settings, model)
    assert len(model.calls) == 0
    assert len(err.value.mcp_trace) == 1


@pytest.mark.asyncio
async def test_insufficient_months_not_silently_shortened(llm_settings):
    model = ModelDouble()
    with pytest.raises(AgentError, match="Недостаточно"):
        await run_agent(ChatRequest(question="Динамика за последние шесть месяцев"), llm_settings, model)
    assert not model.calls


@pytest.mark.asyncio
async def test_two_requested_analyses_produce_two_mcp_results_and_charts(llm_settings):
    first = call("analyze_segment", {"dimension": "region"})
    first["tool_calls"].extend(call("get_delinquency_distribution")["tool_calls"])
    result = await run_agent(ChatRequest(question="Сравни регионы и покажи DPD-корзины"), llm_settings, ModelDouble(first))
    assert len(result["trace"]) == 3
    assert len(result["visualizations"]) == 2
    assert {chart["source_ref"] for chart in result["visualizations"]} == {"T2", "T3"}


@pytest.mark.asyncio
async def test_top_cannot_establish_global_minimum(llm_settings):
    model = ModelDouble(call("get_top_risk_segments", {"dimension": "region", "min_loans": 1}), call("analyze_segment", {"dimension": "region"}))
    result = await run_agent(ChatRequest(question="Сравни регионы с максимальной и минимальной долей"), llm_settings, model)
    assert len(model.calls) == 2
    assert "общий минимум по нему определить нельзя" in result["answer"]
    assert "Минимальное значение" in result["answer"]
    assert result["visualizations"][-1]["source_ref"] == "T3"


@pytest.mark.asyncio
async def test_no_json_text_tool_execution(llm_settings):
    model = ModelDouble(*[{"content": '{"name":"get_portfolio_summary","arguments":{}}'}] * 3)
    with pytest.raises(AgentError) as err:
        await run_agent(ChatRequest(question="Обзор портфеля"), llm_settings, model)
    assert [e["tool"] for e in err.value.mcp_trace] == ["get_dataset_context"]
    assert err.value.usage["llm_requests"] == 3


def test_chart_keeps_missing_values_and_signed_contributions():
    trace = [{"ok": True, "ref": "T2", "tool": "get_npl_trend", "result": {
        "dataset_id": "builtin", "start_date": "2026-08-31", "end_date": "2026-09-30",
        "points": [{"as_of": "2026-08-31", "npl90_ratio": None, "balance_rub": 0},
                   {"as_of": "2026-09-30", "npl90_ratio": .1, "balance_rub": 100}], "npl90_delta_pp": None}}]
    answer, charts, _ = build_report(trace, "builtin")
    assert charts[0]["series"][0]["points"][0]["value"] is None
    assert "не определена" in answer


def test_scope_does_not_discard_unknown_fields():
    tool = next(t for t in list_tools() if t["name"] == "get_portfolio_summary")
    context = {"dates": ["2026-09-30"], "latest_date": "2026-09-30", "filters": {}}
    _, error = resolve_arguments("get_portfolio_summary", {"sql": "SELECT *"}, ChatRequest(question="Обзор"), context, tool)
    assert "Недопустимые параметры MCP" in error


@pytest.mark.asyncio
async def test_two_named_regions_cannot_be_replaced_by_one_summary(llm_settings):
    model = ModelDouble(call("get_portfolio_summary", {"region": "Центральный"}),
                        call("analyze_segment", {"dimension": "region"}))
    result = await run_agent(ChatRequest(question="Сравни Центральный и Приволжский"), llm_settings, model)
    assert len(model.calls) == 2
    assert result["trace"][-1]["tool"] == "analyze_segment"
    assert "Приволжский" in result["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("question, expected", [
    ("А теперь EL на конец этого периода", "2026-08-31"),
    ("А теперь EL на последнюю доступную дату", "2026-09-30"),
])
async def test_followup_snapshot_respects_period_or_explicit_latest(llm_settings, question, expected):
    memory = {"last_scope": {"dataset_id": "builtin", "end_date": "2026-08-31", "start_date": "2026-08-31", "filters": {}}}
    result = await run_agent(ChatRequest(question=question), llm_settings, ModelDouble(call("get_portfolio_summary")), memory=memory)
    assert result["trace"][1]["arguments"]["as_of"] == expected


def test_excluding_category_never_turns_into_including_it():
    tool = next(t for t in list_tools() if t["name"] == "get_portfolio_summary")
    context = {"dates": ["2026-09-30"], "latest_date": "2026-09-30", "filters": {"product": ["Ипотека"]}}
    args, error = resolve_arguments("get_portfolio_summary", {"product": "Ипотека"},
                                    ChatRequest(question="Общий остаток, исключи Ипотеку"), context, tool)
    assert args is None and "Исключение категории не поддерживается" in error


def test_later_snapshot_clears_old_period_scope():
    trace = [{"ok": True, "ref": "T2", "tool": "get_npl_trend", "result": {
        "dataset_id": "builtin", "as_of": "2026-08-31", "start_date": "2026-07-31", "end_date": "2026-08-31",
        "points": [], "npl90_delta_pp": None}},
        {"ok": True, "ref": "T3", "tool": "get_portfolio_summary", "result": {
            "dataset_id": "builtin", "as_of": "2026-09-30", "filters": {}, "metrics": {"balance_rub": 100}}}]
    _, _, scope = build_report(trace, "builtin")
    assert scope["as_of"] == "2026-09-30"
    assert "start_date" not in scope and "end_date" not in scope


@pytest.mark.asyncio
@pytest.mark.parametrize("date_text", ["30 июня 2026", "30.06.2026", "июнь 2026"])
async def test_russian_and_numeric_date_overrides_ui(twelve_month_settings, date_text):
    model = ModelDouble(call("get_portfolio_summary", {"as_of": "2026-06-30"}))
    result = await run_agent(ChatRequest(question=f"Покажи остаток на {date_text}", as_of="2026-09-30"),
                             twelve_month_settings, model)
    assert len(model.calls) == 1
    assert result["trace"][1]["arguments"]["as_of"] == "2026-06-30"
    assert result["resolved_scope"]["as_of"] == "2026-06-30"
    assert "2026-06-30" in result["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["с 30 июня 2026 по 30.09.2026", "с июня 2026 по сентябрь 2026"])
async def test_same_date_parser_resolves_periods(twelve_month_settings, period):
    result = await run_agent(ChatRequest(question=f"Покажи динамику DPD 90+ {period}", as_of="2026-09-30"),
                             twelve_month_settings, ModelDouble(call("get_npl_trend")))
    args = result["trace"][1]["arguments"]
    assert args["start_date"] == "2026-06-30" and args["end_date"] == "2026-09-30"
    assert len(result["trace"][1]["result"]["points"]) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("date_text", ["31 июня 2026", "31.06.2026", "29 июня 2026", "июнь 2030"])
async def test_disallowed_natural_date_rejected_before_model(twelve_month_settings, date_text):
    model = ModelDouble()
    with pytest.raises(AgentError) as err:
        await run_agent(ChatRequest(question=f"Остаток на {date_text}"), twelve_month_settings, model)
    assert "дата" in str(err.value).casefold() or "месяц" in str(err.value).casefold()
    assert model.calls == []
    assert [entry["tool"] for entry in err.value.mcp_trace] == ["get_dataset_context"]


def test_month_only_requires_one_actual_snapshot():
    with pytest.raises(ValueError, match="Неоднозначный месяц"):
        explicit_dates("Остаток на июнь 2026", ["2026-06-15", "2026-06-30"])
    assert explicit_dates("Остаток на июнь 2026", ["2026-06-15"]) == ["2026-06-15"]
