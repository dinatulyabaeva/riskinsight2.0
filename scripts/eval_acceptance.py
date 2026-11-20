"""Acceptance regression with source-row numeric oracles and real MCP.

Controlled provider: exercises orchestration/MCP/rendering, NOT LLM quality.
Live provider: sends only the bundled synthetic dataset's aggregates to the
configured model. No fallback, retries by the eval runner, or model switching.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlparse
import uuid

import httpx

from app.agent import AgentError, ChatCompletionsProvider, ChatRequest, OllamaProvider, run_agent
from app.config import Settings
from app.conversations import ConversationStore
from app.datasets import DatasetStore
from app.db import seed_database
from app.xlsx_import import import_xlsx
from scripts.teaching_dataset import DATES, ROOT, VERSION, contributions, data_digest, records, rounded, select, segments, time_series, totals


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    tool: str
    arguments: dict
    equivalence_group: str = ""
    conversation: str = ""


def cases():
    full = {"start_date": DATES[0], "end_date": DATES[-1]}
    deterioration = {**full, "product": "Потребительский", "region": "Приволжский"}
    recovery = {**full, "product": "Автокредит", "region": "Сибирский"}
    stress = {"as_of": DATES[-1], "pd_multiplier": 1.5, "lgd_add_pp": 5}
    return [
        Case("overview", "На последнюю дату покажи остаток портфеля, долю DPD 90+, её числитель и знаменатель, а также учебный EL.", "get_portfolio_summary", {"as_of": DATES[-1]}, "overview"),
        Case("overview_paraphrase", "Сколько всего денег осталось в портфеле на последнем срезе, какая часть приходится на просрочку от 90 дней и каков EL?", "get_portfolio_summary", {"as_of": DATES[-1]}, "overview"),
        Case("trend_full", "Построй динамику DPD 90+ за весь доступный период. Укажи первую и последнюю даты и изменение в процентных пунктах.", "get_npl_trend", full, "trend_full"),
        Case("trend_paraphrase", "Как менялась доля остатка с просрочкой 90 дней и более с самого первого среза до самого последнего? Нужен график.", "get_npl_trend", full, "trend_full"),
        Case("trend_six", "Покажи тренд доли DPD 90+ только за последние шесть доступных срезов и изменение в п.п.", "get_npl_trend", {"start_date": DATES[-6], "end_date": DATES[-1]}),
        Case("deterioration", "Покажи на графике весь доступный тренд DPD 90+ потребительских кредитов в Приволжском регионе.", "get_npl_trend", deterioration),
        Case("recovery", "Улучшилась ли доля просрочки 90+ по автокредитам в Сибирском регионе с первого месяца до последнего? Покажи график.", "get_npl_trend", recovery),
        Case("stable", "Как менялась доля DPD 90+ по ипотеке во всех регионах за весь период?", "get_npl_trend", {**full, "product": "Ипотека"}),
        Case("regions", "Сравни все регионы по доле DPD 90+ на последнюю дату. Назови максимум и минимум, покажи график.", "analyze_segment", {"as_of": DATES[-1], "dimension": "region", "metric": "npl90_ratio"}),
        Case("concentration", "На последнюю дату сравни продукты по остатку задолженности: в каком продукте сосредоточено больше всего денег?", "analyze_segment", {"as_of": DATES[-1], "dimension": "product", "metric": "balance_rub"}),
        Case("stress", "На последнюю дату увеличь PD в 1,5 раза и LGD на 5 процентных пунктов. Покажи исходный EL, стрессовый EL и изменение.", "run_stress_scenario", stress, "stress"),
        Case("stress_paraphrase", "Оцени сценарий PD ×1,5 и LGD +5 п.п. для последнего среза: сколько составят ожидаемые потери до и после?", "run_stress_scenario", stress, "stress"),
        Case("drivers", "Разложи изменение доли DPD 90+ с 2025-10-31 по 2026-09-30 на вклады продуктов. Покажи внутрисегментный и структурный эффекты.", "explain_npl_change", {**full, "dimension": "product"}),
        Case("distribution", "Как распределён остаток последнего среза по всем корзинам DPD? Покажи доли и график.", "get_delinquency_distribution", {"as_of": DATES[-1]}),
        Case("memory_1", "Построй DPD 90+ по потребительским кредитам в Приволжском регионе за последние шесть доступных срезов.", "get_npl_trend", {**deterioration, "start_date": DATES[-6]}, conversation="segments"),
        Case("memory_2", "А теперь за тот же период по автокредитам в Сибирском регионе.", "get_npl_trend", {**recovery, "start_date": DATES[-6]}, conversation="segments"),
        Case("memory_3", "Покажи тот же график по ипотеке, сохрани регион и период.", "get_npl_trend", {"start_date": DATES[-6], "end_date": DATES[-1], "product": "Ипотека", "region": "Сибирский"}, conversation="segments"),
    ]


class ControlledProvider:
    """Known selections test integration only; no language understanding claim."""
    def __init__(self, case):
        self.case = case
        self.calls = 0

    async def complete(self, messages, schemas, choice):
        self.calls += 1
        if self.calls > 1:
            raise AgentError("Controlled provider expected one valid tool selection; extra model call is a regression")
        advertised = {s["function"]["name"] for s in schemas}
        if self.case.tool not in advertised:
            raise AgentError("Expected analysis tool is absent from the model schema")
        arguments = dict(self.case.arguments)
        # The controlled follow-up deliberately omits inherited values. If the
        # server loses memory, six months silently become twelve or the region
        # becomes all regions, which the independent scope oracle must reject.
        if self.case.name in {"memory_2", "memory_3"}:
            arguments.pop("start_date")
            arguments.pop("end_date")
        if self.case.name == "memory_3":
            arguments.pop("region")
        call = {"id": "controlled_1", "type": "function", "function": {
            "name": self.case.tool, "arguments": json.dumps(arguments, ensure_ascii=False)}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, {}, "tool_calls"


class BudgetProvider:
    def __init__(self, provider, limit):
        self.provider, self.limit, self.calls = provider, limit, 0

    async def complete(self, *args, **kwargs):
        if self.calls >= self.limit:
            raise AgentError("Eval достиг заданного лимита обращений к LLM; новый запрос не отправлен")
        self.calls += 1
        return await self.provider.complete(*args, **kwargs)


def expected_for(case, rows):
    args = case.arguments
    filters = {k: args[k] for k in ("product", "region", "risk_band") if args.get(k) is not None}
    at = args.get("as_of", args.get("end_date", DATES[-1]))
    expected = {"tool": case.tool, "as_of": at, "filters": filters}
    if "start_date" in args:
        expected.update({"start_date": args["start_date"], "end_date": args["end_date"]})
    scope_rows = select(rows, **filters)
    if case.tool == "get_portfolio_summary":
        expected["metrics"] = totals(select(scope_rows, as_of=at))
    elif case.tool == "get_npl_trend":
        points = time_series(rows, **filters)["points"]
        expected["points"] = [p for p in points if args["start_date"] <= p["as_of"] <= args["end_date"]]
        expected["npl90_delta_pp"] = rounded((Decimal(str(expected["points"][-1]["npl90_ratio"])) - Decimal(str(expected["points"][0]["npl90_ratio"]))) * 100, 6)
    elif case.tool == "analyze_segment":
        expected["dimension"], expected["metric"] = args["dimension"], args.get("metric", "npl90_ratio")
        expected["segments"] = sorted(segments(scope_rows, args["dimension"], at), key=lambda r: r[expected["metric"]], reverse=True)
    elif case.tool == "run_stress_scenario":
        expected.update({"pd_multiplier": args["pd_multiplier"], "lgd_add_pp": args["lgd_add_pp"]})
        source = select(scope_rows, as_of=at)
        base = sum((Decimal(str(r["balance_rub"])) * Decimal(str(r["pd_12m"])) * Decimal(str(r["lgd"])) for r in source), Decimal(0))
        stressed = sum((Decimal(str(r["balance_rub"])) * min(Decimal(1), Decimal(str(r["pd_12m"])) * Decimal(str(args["pd_multiplier"])))
                        * min(Decimal(1), Decimal(str(r["lgd"])) + Decimal(str(args["lgd_add_pp"])) / 100) for r in source), Decimal(0))
        expected.update({"base_el_rub": rounded(base), "stressed_el_rub": rounded(stressed), "delta_el_rub": rounded(stressed - base)})
    elif case.tool == "explain_npl_change":
        expected["dimension"] = args["dimension"]
        expected["contributions"] = contributions(scope_rows, args["dimension"], args["start_date"], args["end_date"])
    elif case.tool == "get_delinquency_distribution":
        source = select(scope_rows, as_of=at)
        total = sum((Decimal(str(r["balance_rub"])) for r in source), Decimal(0))
        buckets = []
        for name, low, high in (("0", 0, 0), ("1–29", 1, 29), ("30–59", 30, 59), ("60–89", 60, 89), ("90+", 90, 36500)):
            selected = [r for r in source if low <= r["dpd"] <= high]
            balance = sum((Decimal(str(r["balance_rub"])) for r in selected), Decimal(0))
            buckets.append({"bucket": name, "balance_rub": rounded(balance), "balance_share": rounded(balance / total, 8), "loan_count": len(selected)})
        expected["buckets"] = buckets
    else:
        raise ValueError("Acceptance case has no independent oracle")
    return expected


def _equal_numeric(actual, expected, tolerance=0):
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        raise AssertionError(f"Expected a finite number, got {type(actual).__name__}")
    value = Decimal(str(actual))
    if not value.is_finite() or abs(value - Decimal(str(expected))) > Decimal(str(tolerance)):
        raise AssertionError(f"Numeric mismatch: actual={actual}, expected={expected}, absolute tolerance={tolerance}")


def _check_metrics(actual, expected):
    for key, tolerance in (("balance_rub", .01), ("el_rub", .01), ("npl90_ratio", 1e-8), ("loan_count", 0), ("npl90_count", 0)):
        _equal_numeric(actual[key], expected[key], tolerance)


def check_tool_numbers(data, expected):
    tool = expected["tool"]
    if tool == "get_portfolio_summary":
        _check_metrics(data["metrics"], expected["metrics"])
        for key in ("numerator_kop", "denominator_kop"):
            _equal_numeric(data["npl90_calculation"][key], expected["metrics"][key])
    elif tool == "get_npl_trend":
        assert [p["as_of"] for p in data["points"]] == [p["as_of"] for p in expected["points"]], "Wrong trend dates"
        for actual, golden in zip(data["points"], expected["points"]):
            _check_metrics(actual, golden)
        _equal_numeric(data["npl90_delta_pp"], expected["npl90_delta_pp"], 1e-6)
    elif tool == "analyze_segment":
        assert data["dimension"] == expected["dimension"], "Wrong grouping dimension"
        assert data["sort_metric"] == expected["metric"], "Wrong ranking metric"
        assert [p["segment"] for p in data["segments"]] == [p["segment"] for p in expected["segments"]], "Wrong set or order of segments"
        for actual, golden in zip(data["segments"], expected["segments"]):
            _check_metrics(actual, golden)
    elif tool == "run_stress_scenario":
        for key in ("pd_multiplier", "lgd_add_pp"):
            _equal_numeric(data[key], expected[key])
        for key in ("base_el_rub", "stressed_el_rub", "delta_el_rub"):
            _equal_numeric(data[key], expected[key], .01)
    elif tool == "explain_npl_change":
        assert data["dimension"] == expected["dimension"]
        assert {r["segment"] for r in data["contributions"]} == {r["segment"] for r in expected["contributions"]}
        index = {r["segment"]: r for r in data["contributions"]}
        for row in expected["contributions"]:
            for key in ("within_segment_pp", "mix_pp", "total_contribution_pp"):
                _equal_numeric(index[row["segment"]][key], row[key], 1e-6)
    else:
        assert [p["bucket"] for p in data["buckets"]] == [p["bucket"] for p in expected["buckets"]]
        for actual, golden in zip(data["buckets"], expected["buckets"]):
            for key, tol in (("balance_rub", .01), ("balance_share", 1e-8), ("loan_count", 0)):
                _equal_numeric(actual[key], golden[key], tol)


def chart_oracles(expected):
    tool = expected["tool"]
    series = lambda rows, label, value: [{"label": str(row[label]), "value": row[value]} for row in rows]
    if tool == "get_npl_trend":
        return [("line", "ratio", [series(expected["points"], "as_of", "npl90_ratio")]),
                ("line", "rub", [series(expected["points"], "as_of", "balance_rub")])]
    if tool == "analyze_segment":
        return [("bar", "rub" if expected["metric"].endswith("_rub") else "ratio", [series(expected["segments"], "segment", expected["metric"])])]
    if tool == "get_delinquency_distribution":
        return [("bar", "ratio", [series(expected["buckets"], "bucket", "balance_share")])]
    if tool == "explain_npl_change":
        return [("bar", "pp", [series(expected["contributions"], "segment", "within_segment_pp"), series(expected["contributions"], "segment", "mix_pp")])]
    if tool == "run_stress_scenario":
        return [("bar", "rub", [[{"label": "Исходный", "value": expected["base_el_rub"]}, {"label": "Стрессовый", "value": expected["stressed_el_rub"]}]])]
    return []


def check_charts(charts, expected, reference, dataset_id):
    oracle = chart_oracles(expected)
    matching = [c for c in charts if c.get("source_ref") == reference]
    assert len(matching) == len(oracle), "Expected chart is absent or extra charts use this source"
    for kind, unit, series in oracle:
        candidates = [c for c in matching if c.get("kind") == kind and c.get("unit") == unit]
        assert len(candidates) == 1, "Wrong chart type or units"
        chart = candidates[0]
        assert chart["dataset_id"] == dataset_id, "Chart uses wrong dataset"
        assert chart.get("filters", {}) == expected["filters"], "Chart uses wrong filters"
        for key in ("as_of", "start_date", "end_date"):
            if key in expected:
                assert chart.get(key) == expected[key], f"Wrong chart {key}"
        assert len(chart["series"]) == len(series), "Wrong series count"
        for actual_series, points in zip(chart["series"], series):
            assert len(actual_series["points"]) == len(points), "Wrong point count"
            # Group attribution may tie at zero; align categorical labels, but
            # time series must retain chronological order exactly.
            actual_points = actual_series["points"]
            if kind == "line":
                assert [p["label"] for p in actual_points] == [p["label"] for p in points], "Unordered or wrong dates"
            indexed = {p["label"]: p["value"] for p in actual_points}
            assert set(indexed) == {p["label"] for p in points}, "Wrong chart labels"
            for point in points:
                _equal_numeric(indexed[point["label"]], point["value"], .01 if unit == "rub" else 1e-6 if unit == "pp" else 1e-8)


def _display(value, unit):
    number = Decimal(str(value)) * (100 if unit == "ratio" else 1)
    return f"{number:.2f}".replace("-0.00", "0.00")


def check_answer_numbers(answer, expected):
    # The production renderer owns prose. Require the advertised key figures
    # in the final text independently of the tool/chart reconciliation.
    normalized = re.sub(r"[\s\u00a0\u202f]", "", answer)
    required = []
    if expected["tool"] == "get_portfolio_summary":
        required = [(expected["metrics"][k], "rub") for k in ("balance_rub", "numerator_rub", "denominator_rub", "el_rub")]
        required.append((expected["metrics"]["npl90_ratio"], "ratio"))
    elif expected["tool"] == "get_npl_trend":
        required = [(expected["points"][0]["npl90_ratio"], "ratio"), (expected["points"][-1]["npl90_ratio"], "ratio"), (expected["npl90_delta_pp"], "pp")]
    elif expected["tool"] == "run_stress_scenario":
        required = [(expected[k], "rub") for k in ("base_el_rub", "stressed_el_rub", "delta_el_rub")]
    elif expected["tool"] == "analyze_segment":
        required = [(r[k], unit) for r in expected["segments"] for k, unit in (("balance_rub", "rub"), ("npl90_ratio", "ratio"))]
    elif expected["tool"] == "get_delinquency_distribution":
        required = [(r["balance_share"], "ratio") for r in expected["buckets"]]
    elif expected["tool"] == "explain_npl_change":
        required = [(r["total_contribution_pp"], "pp") for r in expected["contributions"]]
    for value, unit in required:
        token = re.escape(_display(value, unit))
        assert re.search(r"(?<![\d.])" + token + r"(?![\d.])", normalized), f"Required published number absent: {_display(value, unit)} ({unit})"
    assert expected["as_of"] in answer, "Final text omits the snapshot date"
    if "start_date" in expected:
        assert expected["start_date"] in answer, "Final text omits the start date"


def validate_result(result, expected, dataset_id):
    criteria, errors = {}, {}
    def run(name, check):
        try:
            check()
            criteria[name] = True
        except (AssertionError, KeyError, TypeError, ValueError, IndexError) as exc:
            criteria[name], errors[name] = False, str(exc)[:1000]
    if not result:
        return {"passed": False, "criteria": {"completed": False}, "errors": {"completed": "No result"}}
    criteria["completed"] = True
    trace = result.get("trace") or []
    matching = [e for e in trace if e.get("ok") and e.get("tool") == expected["tool"]]
    criteria["expected_mcp_result"] = bool(matching)
    def llm_check():
        assert result.get("usage", {}).get("llm_requests", 0) > 0, "Model provider was not called"
        p = result.get("provenance", {})
        assert p.get("llm_called") is True and p.get("planner") == "llm", "No explicit LLM planning provenance"
        assert p.get("numeric_source") == "mcp", "Numbers are not attributed to MCP"
        assert expected["tool"] in p.get("model_selected_tools", []), "Expected tool was not selected by model provider"
    run("model_planning_provenance", llm_check)
    def citations():
        refs = re.findall(r"\[(T\d+)\]", result.get("answer", ""))
        known = {e.get("ref") for e in trace if e.get("ok") and e.get("tool") != "get_dataset_context"}
        assert refs and all(ref in known for ref in refs), "Absent, failed, context-only, or invented citation"
        assert all(c.get("source_ref") in known for c in result.get("visualizations", [])), "Chart cites no successful analytical source"
    run("citations", citations)
    if matching:
        entry = matching[-1]
        data = entry["result"]
        def scope_check():
            assert all(e.get("result", {}).get("dataset_id", dataset_id) == dataset_id for e in trace if e.get("ok")), "Mixed datasets in trace"
            assert data["dataset_id"] == dataset_id
            assert data.get("filters", {}) == expected["filters"], "Wrong effective data filters"
            scope = result.get("resolved_scope", {})
            assert scope.get("dataset_id") == dataset_id
            assert scope.get("filters", {}) == expected["filters"], "Wrong visible resolved filters"
            for key in ("as_of", "start_date", "end_date"):
                if key in expected:
                    assert data.get(key) == expected[key], f"Wrong MCP {key}"
                    assert scope.get(key) == expected[key], f"Wrong resolved {key}"
        run("requested_scope_and_period", scope_check)
        run("mcp_numeric_oracle", lambda: check_tool_numbers(data, expected))
        run("published_numeric_coverage", lambda: check_answer_numbers(result.get("answer", ""), expected))
        if chart_oracles(expected):
            run("chart_points_units_sources", lambda: check_charts(result.get("visualizations", []), expected, entry["ref"], dataset_id))
        else:
            criteria["chart_points_units_sources"] = None
    else:
        for key in ("requested_scope_and_period", "mcp_numeric_oracle", "published_numeric_coverage", "chart_points_units_sources"):
            criteria[key] = False
        errors["expected_mcp_result"] = f"No successful {expected['tool']} call"
    return {"passed": all(v is not False for v in criteria.values()), "criteria": criteria, "errors": errors}


def numeric_signature(result, case):
    entries = [e for e in result.get("trace", []) if e.get("ok") and e.get("tool") == case.tool]
    if not entries:
        return None
    data = entries[-1]["result"]
    selected = {k: data[k] for k in ("metrics", "points", "npl90_delta_pp", "segments", "buckets", "contributions", "base_el_rub", "stressed_el_rub", "delta_el_rub") if k in data}
    # Tool result calculation objects contain this run's dataset identity.
    canonical = json.dumps(selected, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def evaluate(args):
    if getattr(args, "base_url", None):
        return await evaluate_hosted(args)
    settings = Settings.from_env()
    if args.provider == "live" and (settings.mode != "llm" or not settings.llm_configured):
        raise ValueError("Для live Eval задайте APP_MODE=llm и рабочую конфигурацию LLM. DEMO не включается.")
    active_cases = cases()[:args.limit] if args.limit else cases()
    rows = records()
    report = {"schema_version": 1, "suite": "teacher-acceptance-regression-v1", "provider": args.provider,
              "evaluation_kind": "controlled_provider_integration_not_live_eval" if args.provider == "controlled" else "live_model_regression",
              "created_at": datetime.now(timezone.utc).isoformat(), "repeat": args.repeat,
              "model": "controlled selections" if args.provider == "controlled" else settings.llm_model,
              "api_style": None if args.provider == "controlled" else settings.llm_api_style,
              "dataset": {"version": VERSION, "canonical_data_sha256": data_digest(rows),
                          "xlsx_sha256": hashlib.sha256((ROOT / "examples/portfolio-scenarios.xlsx").read_bytes()).hexdigest(),
                          "rows": len(rows), "dates": list(DATES), "synthetic": True},
              "runtime": {"python": platform.python_version(), "system": platform.system()},
              "agent_limits": {"max_rounds": settings.max_rounds, "max_tool_calls": settings.max_tool_calls,
                               "max_output_tokens": settings.max_output_tokens, "llm_timeout_seconds": settings.llm_timeout,
                               "eval_max_llm_requests": args.max_llm_requests},
              "notes": ["These cases were used during development; this is regression testing, not an independent held-out benchmark.",
                        "A model call does not mean the final numeric text was model-written; inspect provenance.",
                        "Numeric reconciliation covers supported metrics and chart values; it is not semantic verification of arbitrary prose.",
                        "No automatic retry of failed cases, model switching, or fabricated passing answers."], "results": []}
    actual_provider = None
    if args.provider == "live":
        raw = OllamaProvider(settings) if settings.llm_api_style == "ollama" else ChatCompletionsProvider(settings)
        actual_provider = BudgetProvider(raw, args.max_llm_requests)
    with tempfile.TemporaryDirectory(prefix="riskinsight-acceptance-") as temporary:
        path = Path(temporary) / "portfolio.sqlite"
        seed_database(path, 100)
        store = DatasetStore(path)
        store.initialize()
        imported = import_xlsx(store, "portfolio-scenarios.xlsx", (ROOT / "examples/portfolio-scenarios.xlsx").read_bytes())
        if imported["status"] != "imported":
            raise ValueError("Bundled teaching dataset failed validation: " + json.dumps(imported, ensure_ascii=False))
        dataset_id = imported["dataset_id"]
        cfg = replace(settings, db_path=path, mode="llm")
        if args.provider == "controlled":
            cfg = replace(cfg, llm_model="controlled-tool-selections", llm_api_key="controlled-test", llm_api_style="openai")
        signatures = {}
        for repeat in range(1, args.repeat + 1):
            memory_store, conversations = ConversationStore(), {}
            for index, case in enumerate(active_cases, 1):
                print(f"[{repeat}:{index}/{len(active_cases)}] {case.name}", flush=True)
                expected = expected_for(case, rows)
                entry = {"case": case.name, "repeat": repeat, "question": case.question, "expected": expected,
                         "conversation_group": case.conversation or None, "dataset_id": dataset_id}
                request = ChatRequest(question=case.question, dataset_id=dataset_id, as_of=DATES[-1],
                                      conversation_id=conversations.get(case.conversation) if case.conversation else None)
                conversation, _ = memory_store.resolve(request)
                provider = ControlledProvider(case) if actual_provider is None else actual_provider
                started = time.perf_counter()
                before_calls = provider.calls
                try:
                    result = await run_agent(request, cfg, provider=provider, memory=conversation.memory())
                    entry["result"] = result
                    entry["validation"] = validate_result(result, expected, dataset_id)
                    if case.conversation:
                        memory_store.remember(conversation, case.question, result)
                        conversations[case.conversation] = conversation.identifier
                    signature = numeric_signature(result, case)
                    if case.equivalence_group:
                        prior = signatures.setdefault(case.equivalence_group, signature)
                        equal = signature is not None and signature == prior
                        entry["validation"]["criteria"]["paraphrase_repeat_numeric_identity"] = equal
                        if not equal:
                            entry["validation"]["passed"] = False
                            entry["validation"]["errors"]["paraphrase_repeat_numeric_identity"] = "Different numeric evidence for equivalent scope/question"
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"[:2000]
                    entry["partial_trace"] = getattr(exc, "mcp_trace", getattr(exc, "trace", []))
                    entry["validation"] = validate_result(None, expected, dataset_id)
                entry["duration_seconds"] = round(time.perf_counter() - started, 3)
                entry["provider_calls"] = provider.calls - before_calls
                entry["external_model_calls"] = entry["provider_calls"] if args.provider == "live" else 0
                report["results"].append(entry)
                print("  " + ("PASS" if entry["validation"]["passed"] else "FAIL") + f" ({entry['duration_seconds']}s)", flush=True)
                # Checkpoint every case so interruptions/quota errors retain evidence.
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    entries = report["results"]
    traces = [item.get("result", {}).get("trace", item.get("partial_trace", [])) for item in entries]
    successful = sum(item["validation"]["passed"] for item in entries)
    report["summary"] = {"cases": len(entries), "passed_all_criteria": successful, "failed": len(entries) - successful,
                         "failure_rate": (len(entries) - successful) / len(entries),
                         "completed": sum("result" in item for item in entries),
                         "mcp_numeric_oracle_passed": sum(item["validation"]["criteria"].get("mcp_numeric_oracle") is True for item in entries),
                         "chart_oracle_passed": sum(item["validation"]["criteria"].get("chart_points_units_sources") is True for item in entries),
                         "chart_oracle_applicable": sum(bool(chart_oracles(item["expected"])) for item in entries),
                         "external_model_calls": sum(item["external_model_calls"] for item in entries),
                         "controlled_provider_calls": sum(item["provider_calls"] for item in entries) if args.provider == "controlled" else 0,
                         "failed_tool_calls": sum(not t.get("ok") for trace in traces for t in trace),
                         "deterministic_planner_results": sum(item.get("result", {}).get("provenance", {}).get("planner") == "demo" for item in entries),
                         "completed_without_llm_provenance": sum("result" in item and item["result"].get("provenance", {}).get("llm_called") is not True for item in entries),
                         "numeric_text_from_mcp_renderer": sum(item.get("result", {}).get("provenance", {}).get("narrative_source") == "mcp_renderer" for item in entries),
                         "total_seconds": round(sum(item["duration_seconds"] for item in entries), 3)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


class HostedError(ValueError):
    def __init__(self, message, status=None, trace=None, usage=None):
        super().__init__(message)
        self.status = status
        self.trace = trace if isinstance(trace, list) else []
        self.usage = usage if isinstance(usage, dict) else {}


def validate_base_url(value):
    parsed = urlparse(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or parsed.scheme not in {"https", "http"}
            or parsed.scheme == "http" and not local
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("--base-url: требуется HTTPS URL сервиса без токена, пароля, query или fragment; HTTP допустим только localhost")
    return value.rstrip("/")


async def evaluate_hosted(args, *, transport=None):
    """Evaluate deployed HTTP/LLM/MCP, using only the app token locally.

    A mock transport is an explicit test hook. CLI never installs one. Neither
    local LLM settings nor local provider keys determine the hosted model.
    """
    if args.provider != "live":
        raise ValueError("--base-url работает только с --provider live")
    base_url = validate_base_url(args.base_url)
    token = os.getenv("APP_ACCESS_TOKEN", "")
    headers = {"Authorization": "Bearer " + token} if token else {}
    active_cases = cases()[:args.limit] if args.limit else cases()
    rows = records()
    workbook_hash = hashlib.sha256((ROOT / "examples/portfolio-scenarios.xlsx").read_bytes()).hexdigest()
    report = {"schema_version": 1, "suite": "teacher-acceptance-regression-v1", "provider": "live",
              "evaluation_kind": "hosted_live_model_regression", "base_url": base_url,
              "created_at": datetime.now(timezone.utc).isoformat(), "repeat": args.repeat,
              "model": None, "dataset": {"version": VERSION, "canonical_data_sha256": data_digest(rows),
                                           "xlsx_sha256": workbook_hash, "synthetic": True},
              "notes": ["Requests go to the deployed HTTP service; its server-side model configuration is used.",
                        "No local LLM_API_KEY is required or read by this hosted evaluation path.",
                        "Cases are development regression cases, not an independent held-out benchmark.",
                        "No automatic retries, model switching, or DEMO substitution.",
                        "The runner reserves up to 3 model calls per chat request. Failed requests with no usage have unknown actual model-call count.",
                        "Final numbers and charts are checked against an independent source-row Decimal oracle; model prose authorship is not assumed."],
              "results": []}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if token:
            output = output.replace(token, "[REDACTED]")
        args.output.write_text(output, encoding="utf-8")
    def safe_message(message):
        text = str(message)[:2000]
        return text.replace(token, "[REDACTED]") if token else text
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=1200,
                                 follow_redirects=False, trust_env=False, transport=transport) as client:
        async def request_json(method, url, **kwargs):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise HostedError("HTTP connection failed: " + safe_message(type(exc).__name__)) from None
            try:
                data = response.json()
            except ValueError:
                raise HostedError(f"HTTP {response.status_code}: ответ сервиса не является JSON", response.status_code) from None
            if not isinstance(data, dict):
                raise HostedError(f"HTTP {response.status_code}: требуется JSON-объект", response.status_code)
            if not response.is_success:
                raise HostedError(f"HTTP {response.status_code}: " + safe_message(data.get("detail", "Ошибка сервиса")),
                                  response.status_code, data.get("trace", []), data.get("usage", {}))
            return data
        health = await request_json("GET", "/api/health")
        report["server_health_before"] = health
        save()
        if health.get("mode") != "llm" or health.get("llm_configured") is not True:
            raise ValueError("На сервере не подтверждён настроенный режим LLM. Live Eval не запущен.")
        if not all(health.get("features", {}).get(k) for k in ("teaching_dataset", "evidence_charts", "conversation_memory")):
            raise ValueError("Сервер ещё не содержит доработанную версию с учебным датасетом, графиками и памятью.")
        expected_commit = getattr(args, "expected_commit", None)
        if expected_commit and health.get("build_commit") != expected_commit:
            raise ValueError("build_commit сервера не совпадает с --expected-commit. Eval другой версии не запущен.")
        imported = await request_json("POST", "/api/datasets/example")
        if imported.get("source_sha256") != workbook_hash:
            raise ValueError("Учебный XLSX на сервере отличается от локального файла. Не сравниваем разные наборы.")
        dataset_id = imported.get("dataset_id")
        if not isinstance(dataset_id, str) or not re.fullmatch(r"upload_[0-9a-f]{32}", dataset_id):
            raise ValueError("Сервер не вернул корректный ID учебного набора")
        context = await request_json("GET", "/api/context", params={"dataset_id": dataset_id})
        report["server_dataset_context"] = context
        report["model"] = context.get("model")
        if context.get("source_sha256") != workbook_hash or context.get("dates") != list(DATES):
            raise ValueError("MCP-контекст сервера не совпадает с версией и датами контрольного набора")
        if context.get("mode") != "llm" or not context.get("model"):
            raise ValueError("Сервер не сообщает активную LLM-модель для live Eval")
        budget_used = 0
        last_started = None
        signatures = {}
        for repeat in range(1, args.repeat + 1):
            conversations = {}
            for index, case in enumerate(active_cases, 1):
                print(f"[hosted {repeat}:{index}/{len(active_cases)}] {case.name}", flush=True)
                expected = expected_for(case, rows)
                entry = {"case": case.name, "repeat": repeat, "question": case.question, "expected": expected,
                         "conversation_group": case.conversation or None, "dataset_id": dataset_id,
                         "external_model_calls": None, "provider_calls": None, "http_chat_requests": 0}
                request = {"question": case.question, "dataset_id": dataset_id, "as_of": DATES[-1], "filters": {},
                           "client_request_id": uuid.uuid4().hex}
                if case.conversation and case.conversation in conversations:
                    request["conversation_id"] = conversations[case.conversation]
                started = time.perf_counter()
                try:
                    if budget_used + 3 > args.max_llm_requests:
                        entry["external_model_calls"] = entry["provider_calls"] = 0
                        raise HostedError("Eval не отправил запрос: оставшегося бюджета недостаточно для максимум 3 попыток сервера")
                    interval = getattr(args, "min_interval_seconds", 6)
                    if last_started is not None:
                        delay = interval - (time.monotonic() - last_started)
                        if delay > 0:
                            await asyncio.sleep(delay)
                    last_started = time.monotonic()
                    budget_used += 3
                    entry["http_chat_requests"] = 1
                    result = await request_json("POST", "/api/chat", json=request)
                    calls_reported = result.get("usage", {}).get("llm_requests")
                    if isinstance(calls_reported, int) and not isinstance(calls_reported, bool) and 0 <= calls_reported <= 3:
                        budget_used -= 3 - calls_reported
                        entry["external_model_calls"] = entry["provider_calls"] = calls_reported
                    entry["result"] = result
                    entry["validation"] = validate_result(result, expected, dataset_id)
                    consistent = result.get("provenance", {}).get("model") == report["model"]
                    entry["validation"]["criteria"]["server_model_identity"] = consistent
                    if not consistent:
                        entry["validation"]["passed"] = False
                        entry["validation"]["errors"]["server_model_identity"] = "Model identity differs from server context"
                    if case.conversation:
                        cid = result.get("conversation_id")
                        valid_id = isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{32}", cid)
                        continued = bool(valid_id and ("conversation_id" not in request or cid == request["conversation_id"]))
                        entry["validation"]["criteria"]["http_conversation_continuity"] = continued
                        if not continued:
                            entry["validation"]["passed"] = False
                            entry["validation"]["errors"]["http_conversation_continuity"] = "Conversation expired, reset, or ID was absent"
                        if valid_id:
                            conversations[case.conversation] = cid
                    if case.equivalence_group:
                        signature = numeric_signature(result, case)
                        equal = signature is not None and signatures.setdefault(case.equivalence_group, signature) == signature
                        entry["validation"]["criteria"]["paraphrase_numeric_identity"] = equal
                        if not equal:
                            entry["validation"]["passed"] = False
                            entry["validation"]["errors"]["paraphrase_numeric_identity"] = "Different numbers for equivalent question and scope"
                except (HostedError, ValueError, TypeError) as exc:
                    entry["error"] = safe_message(exc)
                    entry["http_status"] = getattr(exc, "status", None)
                    entry["partial_trace"] = getattr(exc, "trace", [])
                    entry["partial_usage"] = getattr(exc, "usage", {})
                    # A failed analytical run can still expose its true model
                    # usage; do not classify that known count as missing.
                    error_calls = entry["partial_usage"].get("llm_requests")
                    if (entry["http_chat_requests"] and entry["external_model_calls"] is None
                            and isinstance(error_calls, int) and not isinstance(error_calls, bool)
                            and 0 <= error_calls <= 3):
                        budget_used -= 3 - error_calls
                        entry["external_model_calls"] = entry["provider_calls"] = error_calls
                    entry["validation"] = validate_result(None, expected, dataset_id)
                entry["duration_seconds"] = round(time.perf_counter() - started, 3)
                report["results"].append(entry)
                save()
                print("  " + ("PASS" if entry["validation"]["passed"] else "FAIL") + f" ({entry['duration_seconds']}s)", flush=True)
        try:
            report["server_health_after"] = await request_json("GET", "/api/health")
            first_commit, last_commit = health.get("build_commit"), report["server_health_after"].get("build_commit")
            report["deployment_unchanged"] = first_commit == last_commit if first_commit and last_commit else None
        except HostedError as exc:
            report["server_health_after_error"] = safe_message(exc)
            report["deployment_unchanged"] = None
    entries = report["results"]
    successes = sum(e["validation"]["passed"] for e in entries)
    unknown_calls = sum(e["external_model_calls"] is None for e in entries)
    known_calls = sum(e["external_model_calls"] or 0 for e in entries)
    report["summary"] = {"cases": len(entries), "passed_all_criteria": successes, "failed": len(entries) - successes,
                         "failure_rate": (len(entries) - successes) / len(entries),
                         "completed": sum("result" in e for e in entries),
                         "mcp_numeric_oracle_passed": sum(e["validation"]["criteria"].get("mcp_numeric_oracle") is True for e in entries),
                         "chart_oracle_passed": sum(e["validation"]["criteria"].get("chart_points_units_sources") is True for e in entries),
                         "chart_oracle_applicable": sum(bool(chart_oracles(e["expected"])) for e in entries),
                         "external_model_calls": None if unknown_calls else known_calls,
                         "external_model_calls_known_total": known_calls, "requests_with_unknown_model_call_count": unknown_calls,
                         "http_chat_requests": sum(e["http_chat_requests"] for e in entries),
                         "budget_calls_used_or_reserved": budget_used,
                         "failed_tool_calls": sum(not t.get("ok") for e in entries for t in e.get("result", {}).get("trace", e.get("partial_trace", []))),
                         "completed_without_llm_provenance": sum("result" in e and e["result"].get("provenance", {}).get("llm_called") is not True for e in entries),
                         "numeric_text_from_mcp_renderer": sum(e.get("result", {}).get("provenance", {}).get("narrative_source") == "mcp_renderer" for e in entries),
                         "total_seconds": round(sum(e["duration_seconds"] for e in entries), 3)}
    if report.get("deployment_unchanged") is False:
        report["summary"]["release_check_failed"] = True
        report["notes"].append("Server commit changed during the evaluation; do not attribute the suite to one release.")
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("controlled", "live"), default="controlled")
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-llm-requests", type=int, default=40)
    parser.add_argument("--base-url", help="Evaluate the deployed service with APP_ACCESS_TOKEN; only for --provider live")
    parser.add_argument("--expected-commit", help="Optional exact deployed Git commit required before sending chat requests")
    parser.add_argument("--min-interval-seconds", type=float, default=6,
                        help="Minimum interval between hosted chat requests; protects the service's per-minute budget")
    parser.add_argument("--output", type=Path, default=ROOT / "eval_results/acceptance.json")
    args = parser.parse_args()
    if args.limit is not None and not 1 <= args.limit <= len(cases()):
        parser.error(f"--limit must be between 1 and {len(cases())}")
    if args.max_llm_requests < 1:
        parser.error("--max-llm-requests must be positive")
    if args.base_url and args.provider != "live":
        parser.error("--base-url requires --provider live")
    if args.min_interval_seconds < 0 or args.min_interval_seconds > 60:
        parser.error("--min-interval-seconds must be between 0 and 60")
    try:
        report = asyncio.run(evaluate(args))
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {args.output}")
    if report["summary"]["failed"] or report["summary"].get("release_check_failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
