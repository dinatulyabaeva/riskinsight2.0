"""Reproducible teaching records and an independent Decimal calculation oracle.

No Analytics/SQLite imports: the oracle calculates directly from the rows that
are exported to XLSX. Deliberate scenarios are specified, not fitted to markets.
"""
from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path

VERSION = "teaching-v1"
COLUMNS = ("loan_id", "as_of", "product", "region", "risk_band", "balance_rub", "dpd", "pd_12m", "lgd")
PRODUCTS = ("Потребительский", "Ипотека", "Автокредит", "Кредитная карта")
REGIONS = ("Центральный", "Приволжский", "Сибирский")
DATES = tuple(f"{2025 + (month - 1) // 12}-{(month - 1) % 12 + 1:02d}-{calendar.monthrange(2025 + (month - 1) // 12, (month - 1) % 12 + 1)[1]:02d}" for month in range(10, 22))
ROOT = Path(__file__).resolve().parents[1]


def records():
    """1,200 distinct loans, a fixed panel of twelve month-end snapshots."""
    rows = []
    principals = (600_000, 3_000_000, 1_200_000, 120_000)
    base_pd = (Decimal("0.020"), Decimal("0.005"), Decimal("0.012"), Decimal("0.040"))
    base_lgd = (Decimal("0.60"), Decimal("0.20"), Decimal("0.40"), Decimal("0.85"))
    for month, as_of in enumerate(DATES):
        for product_index, product in enumerate(PRODUCTS):
            for region_index, region in enumerate(REGIONS):
                for index in range(100):
                    # Same relative amortization in every cohort: changing DPD90
                    # is observable without confounding changes in segment weight.
                    principal = principals[product_index] + index * 1000 + region_index * 5000
                    balance = principal * (100 - month) // 100
                    persistent = (4, 2, 4, 8)[product_index]
                    dpd = 120 + 30 * month if index < persistent else 0
                    if product == "Потребительский" and region == "Приволжский":
                        # 2 defaults until March; four more cross 90 DPD each
                        # month from April through September (26 by the end).
                        dpd = 120 + 30 * month if index < 2 else 0
                        if 2 <= index < 26:
                            cross_month = 6 + (index - 2) // 4
                            dpd = max(0, 90 + 30 * (month - cross_month))
                    elif product == "Автокредит" and region == "Сибирский":
                        # Twenty initial defaults, sixteen cures in April–Sep.
                        if index < 20:
                            cure_month = 99 if index < 4 else 6 + min(5, (index - 4) // 3)
                            dpd = 120 + 30 * month if month < cure_month else 0
                    # A small fixed early-delinquency group makes bucket charts
                    # useful without affecting the planted DPD90 trajectories.
                    if index in (90, 91, 92) and dpd == 0:
                        dpd = (15, 45, 75)[index - 90]
                    pd = base_pd[product_index] + Decimal(index % 4) * Decimal("0.005")
                    if dpd >= 90:
                        pd = Decimal(1)
                    elif dpd:
                        pd += Decimal(dpd) / Decimal(1000)
                    # Two high-LGD card exposures exercise the stress cap at 1.
                    lgd = Decimal("0.98") if product_index == 3 and index < 2 else base_lgd[product_index]
                    rows.append({"loan_id": f"TST-{product_index + 1}{region_index + 1}-{index + 1:04d}",
                                 "as_of": as_of, "product": product, "region": region,
                                 "risk_band": "ABCD"[index % 4], "balance_rub": balance,
                                 "dpd": dpd, "pd_12m": float(pd), "lgd": float(lgd)})
    return rows


def data_digest(rows):
    """Hash logical cell values, independent of ZIP timestamps and formatting."""
    canonical = json.dumps([[row[column] for column in COLUMNS] for row in rows],
                           ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def rounded(value, digits=2):
    return float(value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN))


def _decimal(value):
    return Decimal(str(value))


def select(rows, *, as_of=None, **filters):
    return [r for r in rows if (as_of is None or r["as_of"] == as_of)
            and all(r.get(k) == v for k, v in filters.items() if v is not None)]


def totals(rows):
    balance = sum((_decimal(r["balance_rub"]) for r in rows), Decimal(0))
    overdue = sum((_decimal(r["balance_rub"]) for r in rows if r["dpd"] >= 90), Decimal(0))
    el = sum((_decimal(r["balance_rub"]) * _decimal(r["pd_12m"]) * _decimal(r["lgd"]) for r in rows), Decimal(0))
    stressed = sum((_decimal(r["balance_rub"]) * min(Decimal(1), _decimal(r["pd_12m"]) * Decimal("1.5"))
                    * min(Decimal(1), _decimal(r["lgd"]) + Decimal("0.05")) for r in rows), Decimal(0))
    return {"loan_count": len(rows), "balance_rub": rounded(balance),
            "numerator_rub": rounded(overdue), "denominator_rub": rounded(balance),
            "numerator_kop": int(overdue * 100), "denominator_kop": int(balance * 100),
            "npl90_ratio": rounded(overdue / balance, 8) if balance else None,
            "npl90_count": sum(r["dpd"] >= 90 for r in rows),
            "el_rub": rounded(el), "base_el_rub": rounded(el),
            "stressed_el_rub": rounded(stressed), "delta_el_rub": rounded(stressed - el)}


def time_series(rows, **filters):
    points = [{"as_of": date, **totals(select(rows, as_of=date, **filters))} for date in DATES]
    first, last = points[0], points[-1]
    delta = ((_decimal(last["numerator_rub"]) / _decimal(last["denominator_rub"])) -
             (_decimal(first["numerator_rub"]) / _decimal(first["denominator_rub"]))) * 100
    # Match the declared precision of tool trend points (8 decimal fractions).
    displayed_delta = (_decimal(last["npl90_ratio"]) - _decimal(first["npl90_ratio"])) * 100
    return {"filters": filters, "start_date": DATES[0], "end_date": DATES[-1],
            "points": points, "npl90_delta_pp": rounded(displayed_delta, 6),
            "unrounded_ratio_delta_pp": rounded(delta, 8)}


def segments(rows, dimension, as_of=DATES[-1]):
    current = select(rows, as_of=as_of)
    grand_balance = sum((_decimal(r["balance_rub"]) for r in current), Decimal(0))
    groups = defaultdict(list)
    for row in current:
        groups[row[dimension]].append(row)
    result = []
    for name, group in groups.items():
        summary = totals(group)
        result.append({"segment": name, **summary,
                       "portfolio_share": rounded(_decimal(summary["balance_rub"]) / grand_balance, 8)})
    return sorted(result, key=lambda r: r["npl90_ratio"], reverse=True)


def contributions(rows, dimension="product", start_date=DATES[0], end_date=DATES[-1]):
    sides = []
    for date in (start_date, end_date):
        current = select(rows, as_of=date)
        total = sum((_decimal(r["balance_rub"]) for r in current), Decimal(0))
        groups = defaultdict(list)
        for row in current:
            groups[row[dimension]].append(row)
        side = {}
        for segment, group in groups.items():
            metrics = totals(group)
            b, n = _decimal(metrics["balance_rub"]), _decimal(metrics["numerator_rub"])
            side[segment] = (b / total, n / b)
        sides.append(side)
    result = []
    for name in sides[0]:
        w0, r0 = sides[0][name]
        w1, r1 = sides[1][name]
        within = (w0 + w1) / 2 * (r1 - r0) * 100
        mix = (r0 + r1) / 2 * (w1 - w0) * 100
        result.append({"segment": name, "within_segment_pp": rounded(within, 6),
                       "mix_pp": rounded(mix, 6), "total_contribution_pp": rounded(within + mix, 6)})
    return sorted(result, key=lambda r: r["total_contribution_pp"], reverse=True)


def expected_results(rows=None):
    rows = records() if rows is None else rows
    return {"version": VERSION, "synthetic": True, "seed": None,
            "generation": "Deterministic specified cohorts; no stochastic sampling",
            "canonical_data_sha256": data_digest(rows), "row_count": len(rows),
            "loan_count": 1200, "dates": list(DATES), "currency": "RUB",
            "calculation": "Independent Python Decimal over source rows; no SQL or app.analytics",
            "money_tolerance_rub": 0.01, "fraction_tolerance": 0.00000001,
            "latest": {"as_of": DATES[-1], **totals(select(rows, as_of=DATES[-1]))},
            "trend": time_series(rows),
            "scenarios": {
                "deterioration": time_series(rows, product="Потребительский", region="Приволжский"),
                "recovery": time_series(rows, product="Автокредит", region="Сибирский"),
                "stable": time_series(rows, product="Ипотека"),
            },
            "regions": segments(rows, "region"), "products": segments(rows, "product"),
            "contributions": contributions(rows),
            "stress": {"pd_multiplier": 1.5, "lgd_add_pp": 5,
                       **{k: v for k, v in totals(select(rows, as_of=DATES[-1])).items()
                          if k in {"base_el_rub", "stressed_el_rub", "delta_el_rub"}}},
            "limits": ["Synthetic fixed panel; no real borrowers or causal inference",
                       "DPD90 is balance-weighted, not share of loan count",
                       "EL is an educational proxy, not IFRS 9 ECL",
                       "Stress holds EAD fixed and clips PD/LGD at one",
                       "Balances are stocks; do not sum snapshots over dates"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-output", type=Path, default=ROOT / "examples/portfolio-scenarios.expected.json")
    parser.add_argument("--data-output", type=Path, help="Optional temporary JSON input for build_teaching_workbook.mjs")
    args = parser.parse_args()
    rows = records()
    expected = expected_results(rows)
    args.expected_output.parent.mkdir(parents=True, exist_ok=True)
    args.expected_output.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.data_output:
        args.data_output.parent.mkdir(parents=True, exist_ok=True)
        args.data_output.write_text(json.dumps({"columns": COLUMNS, "rows": [[r[c] for c in COLUMNS] for r in rows]},
                                               ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "loans": expected["loan_count"], "dates": len(DATES),
                      "canonical_data_sha256": expected["canonical_data_sha256"],
                      "latest": expected["latest"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
