"""Regression: top-N regional ranking vs full max/min comparison."""
from app.agent import _render_matching_mcp_evidence


def _trace(tool, segments):
    return [{"ok": True, "tool": tool, "ref": "T2", "result": {
        "dimension": "region", "segments": segments}}]


def test_top_ranking_from_top_risk_segments():
    trace = _trace("get_top_risk_segments", [
        {"segment": "A", "npl90_ratio": 0.08, "balance_rub": 2000},
        {"segment": "B", "npl90_ratio": 0.04, "balance_rub": 3000},
    ])
    result = _render_matching_mcp_evidence(
        "Какие регионы имеют максимальную долю DPD 90+?", trace, "builtin")
    assert result is not None
    assert "Рейтинг регионов" in result
    assert "8.00%" in result
    assert "[T2]" in result


def test_top_only_cannot_claim_minimum():
    trace = _trace("get_top_risk_segments", [
        {"segment": "A", "npl90_ratio": 0.08, "balance_rub": 2000},
        {"segment": "B", "npl90_ratio": 0.04, "balance_rub": 3000},
    ])
    assert _render_matching_mcp_evidence(
        "Сравни регионы: укажи максимальную и минимальную долю DPD 90+", trace, "builtin") is None


def test_full_segment_comparison_reports_both_extremes():
    trace = _trace("analyze_segment", [
        {"segment": "A", "npl90_ratio": 0.08, "balance_rub": 2000},
        {"segment": "B", "npl90_ratio": 0.04, "balance_rub": 3000},
    ])
    result = _render_matching_mcp_evidence(
        "Сравни регионы: укажи максимальную и минимальную долю DPD 90+", trace, "builtin")
    assert "Максимальная доля: A" in result
    assert "Минимальная доля: B" in result
