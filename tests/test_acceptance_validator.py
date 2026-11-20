"""The acceptance checker must reject numerical and provenance regressions."""
from copy import deepcopy

import pytest

from app.evidence import build_report
from app.mcp_client import MCPClient, EvidenceSession
from scripts.eval_acceptance import cases, expected_for, validate_result
from scripts.teaching_dataset import records
from test_teaching_dataset import teaching_import  # shared genuine XLSX import fixture


@pytest.fixture
async def reference(teaching_import):
    builtin, dataset_id = teaching_import
    case = cases()[2]
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        session = EvidenceSession(client)
        await session.call("get_dataset_context")
        await session.call(case.tool, case.arguments)
    answer, charts, scope = build_report(session.trace, dataset_id)
    result = {"answer": answer, "trace": session.trace, "visualizations": charts,
              "resolved_scope": scope, "usage": {"llm_requests": 1},
              "provenance": {"llm_called": True, "planner": "llm", "numeric_source": "mcp",
                             "narrative_source": "mcp_renderer", "model_selected_tools": [case.tool]}}
    return result, expected_for(case, records()), dataset_id


@pytest.mark.asyncio
async def test_validator_accepts_reconciled_result(reference):
    result, expected, dataset_id = reference
    validation = validate_result(result, expected, dataset_id)
    assert validation["passed"], validation


@pytest.mark.asyncio
@pytest.mark.parametrize("defect,criterion", [
    ("wrong_numeric", "mcp_numeric_oracle"),
    ("wrong_filter", "requested_scope_and_period"),
    ("wrong_period", "requested_scope_and_period"),
    ("chart_invented_point", "chart_points_units_sources"),
    ("chart_percent_instead_of_fraction", "chart_points_units_sources"),
    ("chart_missing_date", "chart_points_units_sources"),
    ("chart_wrong_unit", "chart_points_units_sources"),
    ("chart_invalid_source", "citations"),
    ("no_model_called", "model_planning_provenance"),
    ("missing_numerical_answer", "published_numeric_coverage"),
    ("invented_citation", "citations"),
])
async def test_validator_rejects_real_defects(reference, defect, criterion):
    pristine, expected, dataset_id = reference
    result = deepcopy(pristine)
    data = result["trace"][-1]["result"]
    if defect == "wrong_numeric":
        data["points"][-1]["balance_rub"] += 1  # Reject one ruble, even on a billion-ruble balance.
    elif defect == "wrong_filter":
        data["filters"] = {"region": "Центральный"}
    elif defect == "wrong_period":
        data["start_date"] = expected["points"][1]["as_of"]
    elif defect == "chart_invented_point":
        result["visualizations"][0]["series"][0]["points"][0]["value"] = .9
    elif defect == "chart_percent_instead_of_fraction":
        result["visualizations"][0]["series"][0]["points"][0]["value"] *= 100
    elif defect == "chart_missing_date":
        result["visualizations"][0]["series"][0]["points"].pop()
    elif defect == "chart_wrong_unit":
        result["visualizations"][0]["unit"] = "rub"
    elif defect == "chart_invalid_source":
        result["visualizations"][0]["source_ref"] = "T99"
    elif defect == "no_model_called":
        result["usage"]["llm_requests"] = 0
        result["provenance"]["llm_called"] = False
    elif defect == "missing_numerical_answer":
        result["answer"] = "Анализ выполнен. [T2]"
    elif defect == "invented_citation":
        result["answer"] += " Вывод по другому источнику [T99]."
    validation = validate_result(result, expected, dataset_id)
    assert not validation["passed"]
    assert validation["criteria"][criterion] is False, validation
