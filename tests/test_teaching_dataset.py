"""The shipped multi-period XLSX is reconciled against an independent oracle."""
import hashlib
import json
from pathlib import Path

import pytest

from app.datasets import DatasetStore
from app.db import seed_database
from app.mcp_client import MCPClient
from app.xlsx_import import import_xlsx, validate_xlsx
from scripts.teaching_dataset import COLUMNS, DATES, data_digest, expected_results, records

ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = ROOT / "examples/portfolio-scenarios.xlsx"
EXPECTED = json.loads((ROOT / "examples/portfolio-scenarios.expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def teaching_import(tmp_path_factory):
    directory = tmp_path_factory.mktemp("teaching")
    builtin = directory / "portfolio.sqlite"
    seed_database(builtin, n_loans=100)
    before = hashlib.sha256(builtin.read_bytes()).hexdigest()
    store = DatasetStore(builtin)
    store.initialize()
    report = import_xlsx(store, WORKBOOK.name, WORKBOOK.read_bytes())
    assert report["status"] == "imported", report
    assert report["row_count"] == report["valid_rows"] == 14400
    assert hashlib.sha256(builtin.read_bytes()).hexdigest() == before
    return builtin, report["dataset_id"]


def assert_metrics(actual, expected):
    for key in ("balance_rub", "el_rub"):
        assert actual[key] == pytest.approx(expected[key], abs=.01, rel=0), key
    assert actual["npl90_ratio"] == pytest.approx(expected["npl90_ratio"], abs=1e-8, rel=0)
    assert actual["loan_count"] == expected["loan_count"]
    assert actual["npl90_count"] == expected["npl90_count"]


def test_shipped_workbook_matches_deterministic_records_and_oracle():
    report, imported = validate_xlsx(WORKBOOK.read_bytes())
    assert report["error_count"] == 0, report
    assert len(imported) == EXPECTED["row_count"] == 14400
    # validate_xlsx normalizes money to kopecks. Recover logical source values
    # before comparing; all fixture balances happen to be exact whole rubles.
    logical = [{**r, "balance_rub": r["balance_rub"] // 100 if r["balance_rub"] % 100 == 0 else r["balance_rub"] / 100} for r in imported]
    assert data_digest(logical) == EXPECTED["canonical_data_sha256"]
    assert records() == logical
    assert expected_results() == EXPECTED
    assert len({(r["loan_id"], r["as_of"]) for r in logical}) == len(logical)
    assert all(set(r) == set(COLUMNS) for r in logical)


def test_scenarios_are_distinct_and_not_just_different_prompts():
    worsening = EXPECTED["scenarios"]["deterioration"]["points"]
    recovering = EXPECTED["scenarios"]["recovery"]["points"]
    stable = EXPECTED["scenarios"]["stable"]["points"]
    assert worsening[0]["npl90_ratio"] < worsening[-1]["npl90_ratio"]
    assert recovering[0]["npl90_ratio"] > recovering[-1]["npl90_ratio"]
    assert len({p["npl90_ratio"] for p in stable}) == 1
    assert all(a["npl90_ratio"] <= b["npl90_ratio"] for a, b in zip(worsening, worsening[1:]))
    assert all(a["npl90_ratio"] >= b["npl90_ratio"] for a, b in zip(recovering, recovering[1:]))
    # Concentration is a different question from ranking overdue percentages.
    products = EXPECTED["products"]
    assert max(products, key=lambda p: p["balance_rub"])["segment"] == "Ипотека"
    assert max(products, key=lambda p: p["npl90_ratio"])["segment"] != "Ипотека"


@pytest.mark.asyncio
async def test_teaching_summary_and_every_trend_point_through_real_mcp(teaching_import):
    builtin, dataset_id = teaching_import
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        context = await client.call_tool("get_dataset_context", {})
        assert context["dates"] == list(DATES)
        assert context["loan_count"] == 1200
        summary = await client.call_tool("get_portfolio_summary", {})
        assert_metrics(summary["metrics"], EXPECTED["latest"])
        calc = summary["npl90_calculation"]
        for key in ("numerator_kop", "denominator_kop", "numerator_rub", "denominator_rub"):
            assert calc[key] == EXPECTED["latest"][key]
        trend = await client.call_tool("get_npl_trend", {"start_date": DATES[0], "end_date": DATES[-1]})
        assert len(trend["points"]) == 12
        for actual, expected in zip(trend["points"], EXPECTED["trend"]["points"]):
            assert actual["as_of"] == expected["as_of"]
            assert_metrics(actual, expected)
        assert trend["npl90_delta_pp"] == pytest.approx(EXPECTED["trend"]["npl90_delta_pp"], abs=1e-6, rel=0)
        again = await client.call_tool("get_portfolio_summary", {})
        assert again == summary
        assert summary["dataset_id"] == dataset_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ("deterioration", "recovery", "stable"))
async def test_each_planted_scenario_through_real_mcp(teaching_import, scenario):
    builtin, dataset_id = teaching_import
    expected = EXPECTED["scenarios"][scenario]
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        data = await client.call_tool("get_npl_trend", {"start_date": DATES[0], "end_date": DATES[-1], **expected["filters"]})
    assert data["filters"] == expected["filters"]
    assert len(data["points"]) == len(expected["points"])
    for point, golden in zip(data["points"], expected["points"]):
        assert_metrics(point, golden)


@pytest.mark.asyncio
async def test_rankings_stress_and_decomposition_reconcile(teaching_import):
    builtin, dataset_id = teaching_import
    async with MCPClient(builtin, dataset_id=dataset_id) as client:
        for dimension, key in (("region", "regions"), ("product", "products")):
            response = await client.call_tool("analyze_segment", {"dimension": dimension})
            assert [s["segment"] for s in response["segments"]] == [s["segment"] for s in EXPECTED[key]]
            for actual, golden in zip(response["segments"], EXPECTED[key]):
                assert_metrics(actual, golden)
                assert actual["portfolio_share"] == pytest.approx(golden["portfolio_share"], abs=1e-8, rel=0)
        stress = await client.call_tool("run_stress_scenario", {"pd_multiplier": 1.5, "lgd_add_pp": 5})
        for key in ("base_el_rub", "stressed_el_rub", "delta_el_rub"):
            assert stress[key] == pytest.approx(EXPECTED["stress"][key], abs=.01, rel=0), key
        drivers = await client.call_tool("explain_npl_change", {"start_date": DATES[0], "end_date": DATES[-1], "dimension": "product"})
        for actual, golden in zip(drivers["contributions"], EXPECTED["contributions"]):
            assert actual["segment"] == golden["segment"]
            for key in ("within_segment_pp", "mix_pp", "total_contribution_pp"):
                assert actual[key] == pytest.approx(golden[key], abs=1e-6, rel=0)
        assert sum(row["total_contribution_pp"] for row in drivers["contributions"]) == pytest.approx(drivers["npl90_delta_pp"], abs=4e-6, rel=0)
