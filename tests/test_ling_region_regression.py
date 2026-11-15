"""Regression: Ling selects analyze_segment but writes an invalid final."""
from app.agent import _render_matching_mcp_evidence


def _trace(dimension="region"):
    return [
        {"ok": True, "tool": "get_dataset_context", "ref": "T1", "result": {}},
        {"ok": True, "tool": "analyze_segment", "ref": "T2", "result": {
            "dimension": dimension,
            "segments": [
                {"segment": "Север", "npl90_ratio": 0.12, "balance_rub": 1000},
                {"segment": "Юг", "npl90_ratio": 0.02, "balance_rub": 2000},
                {"segment": "Запад", "npl90_ratio": 0.07, "balance_rub": 3000},
            ],
        }},
    ]


def test_region_max_min_uses_analyze_segment():
    answer = _render_matching_mcp_evidence(
        "Сравни долю DPD 90+ по регионам. Укажи регионы с максимальной и минимальной долей",
        _trace(), "builtin"
    )
    assert "Максимальная доля: Север — 12.00%" in answer
    assert "Минимальная доля: Юг — 2.00%" in answer
    assert "[T2]" in answer


def test_wrong_dimension_cannot_support_region_comparison():
    assert _render_matching_mcp_evidence("Сравни регионы по DPD 90+", _trace("product"), "builtin") is None


def test_top_only_cannot_claim_minimum():
    trace = [{"ok": True, "tool": "get_top_risk_segments", "ref": "T2", "result": {
        "dimension": "region", "segments": [{"segment": "Север", "npl90_ratio": .12, "balance_rub": 1000}]
    }}]
    assert _render_matching_mcp_evidence("Максимальная и минимальная доля по регионам", trace, "builtin") is None
