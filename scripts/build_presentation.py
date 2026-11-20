"""Rebuild the standalone HTML deck from the independently checked fixture.

No model or network is called. Offline slide data is explicitly labeled and is
not a saved or live LLM answer.
"""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build():
    expected = json.loads((ROOT / "examples/portfolio-scenarios.expected.json").read_text(encoding="utf-8"))
    template = (ROOT / "docs/presentation.template.html").read_text(encoding="utf-8")
    def number(value, decimals=2):
        return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")
    def percent(value, decimals=2):
        return number(value * 100, decimals) + "%"
    trend, scenarios, latest = expected["trend"], expected["scenarios"], expected["latest"]
    replacements = {
        "TOTAL_FIRST": percent(trend["points"][0]["npl90_ratio"]),
        "TOTAL_LAST": percent(trend["points"][-1]["npl90_ratio"]),
        "TOTAL_LAST_PRECISE": percent(trend["points"][-1]["npl90_ratio"], 6),
        "NUMERATOR": number(latest["numerator_rub"]),
        "DENOMINATOR": number(latest["denominator_rub"]),
        "EL_BASE": number(expected["stress"]["base_el_rub"]),
        "EL_STRESS": number(expected["stress"]["stressed_el_rub"]),
        "EL_DELTA": number(expected["stress"]["delta_el_rub"]),
        "EL_BASE_MLN": number(expected["stress"]["base_el_rub"] / 1e6),
        "EL_STRESS_MLN": number(expected["stress"]["stressed_el_rub"] / 1e6),
        "EL_DELTA_MLN": number(expected["stress"]["delta_el_rub"] / 1e6),
    }
    for name, key in (("STRESS", "deterioration"), ("RECOVERY", "recovery"), ("STABLE", "stable")):
        replacements[name + "_FIRST"] = percent(scenarios[key]["points"][0]["npl90_ratio"])
        replacements[name + "_LAST"] = percent(scenarios[key]["points"][-1]["npl90_ratio"])
    for key, value in replacements.items():
        template = template.replace("__" + key + "__", value)
    template = template.replace("__FIXTURE_DATA__", json.dumps(expected, ensure_ascii=False).replace("</", "<\\/"))
    target = ROOT / "docs/presentation.html"
    target.write_text(template, encoding="utf-8")
    print(f"Created {target.relative_to(ROOT)} from {expected['version']}")


if __name__ == "__main__":
    build()
