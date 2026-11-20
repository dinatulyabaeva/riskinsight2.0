"""Verified reports and chart data from actual MCP results, never from LLM prose.

The model selects tools. This module has no database or model access: it formats
the structured results already present in the current request's evidence trace.
"""
import math


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def amount(value):
    return "не рассчитан" if value is None else f"{value:,.2f} ₽".replace(",", " ")


def percent(value):
    return "не определена" if value is None else f"{value * 100:.2f}%"


def signed_pp(value):
    return "не определено" if value is None else f"{value:+.2f} п.п."


def _scope(data, dataset_id):
    scope = {"dataset_id": dataset_id, "as_of": data.get("as_of"),
             "filters": dict(data.get("filters") or {})}
    for key in ("start_date", "end_date"):
        if data.get(key):
            scope[key] = data[key]
    return scope


def _scope_text(data, dataset_id):
    period = (f"{data['start_date']} — {data['end_date']}"
              if data.get("start_date") and data.get("end_date") else data.get("as_of", "весь набор"))
    filters = "; ".join(f"{key}={value}" for key, value in (data.get("filters") or {}).items()) or "все продукты и регионы"
    return f"Набор: {dataset_id}. Период/срез: {period}. Область: {filters}."


def _chart(kind, title, unit, series, data, reference, dataset_id, note=""):
    """A closed, executable-code-free chart contract. Null points stay null."""
    for item in series:
        for point in item["points"]:
            value = point["value"]
            if value is not None and not number(value):
                raise ValueError("MCP вернул нечисловую точку графика")
            if unit == "ratio" and value is not None and not 0 <= value <= 1:
                raise ValueError("MCP вернул долю вне диапазона 0..1")
    return {"kind": kind, "title": title, "x_label": "Дата среза" if kind == "line" else "Категория",
            "y_label": {"ratio": "Доля, %", "rub": "Рубли", "pp": "Изменение, п.п."}[unit],
            "unit": unit, "series": series, "source_ref": reference,
            **_scope(data, dataset_id), "note": note}


def _series(name, rows, label_key, value_key):
    return {"name": name, "points": [{"label": str(row[label_key]), "value": row.get(value_key)} for row in rows]}


def build_report(trace, dataset_id):
    """Return a grounded report, charts, and the last effective analytic scope.

    Each numerical paragraph and every chart references the actual current
    trace. A top-N response is never treated as a complete population.
    """
    sections, charts = [], []
    last_scope = {"dataset_id": dataset_id, "as_of": None, "filters": {}}
    for entry in trace:
        if not entry.get("ok") or entry.get("tool") == "get_dataset_context":
            continue
        data, ref, tool = entry.get("result"), entry.get("ref"), entry.get("tool")
        if not isinstance(data, dict) or not ref:
            raise ValueError("Некорректная запись источника MCP")
        if data.get("dataset_id", dataset_id) != dataset_id:
            raise ValueError("Источник MCP относится к другому набору")
        lines = [_scope_text(data, dataset_id)]
        if tool != "check_data_quality":
            current_scope = _scope(data, dataset_id)
            if "start_date" not in current_scope and current_scope.get("as_of") == last_scope.get("end_date"):
                current_scope.update({key: last_scope[key] for key in ("start_date", "end_date") if key in last_scope})
            last_scope = current_scope
        if tool == "get_portfolio_summary":
            m, calc = data["metrics"], data.get("npl90_calculation", {})
            lines += [f"Остаток портфеля: {amount(m.get('balance_rub'))}. Кредитов: {m.get('loan_count', 'нет данных')}.",
                      f"Доля DPD 90+: {percent(m.get('npl90_ratio'))}."]
            if calc:
                lines.append(f"Числитель: {amount(calc.get('numerator_rub'))}; знаменатель: {amount(calc.get('denominator_rub'))}. "
                             "Формула: SUM(balance_kop WHERE dpd >= 90) / SUM(balance_kop).")
            lines.append(f"Учебный EL: {amount(m.get('el_rub'))}. Формула: Σ(EAD × PD12 × LGD). Это не IFRS 9 ECL и не бухгалтерский резерв.")
            if m.get("unavailable_metrics"):
                lines.append("Недоступные показатели: " + "; ".join(f"{key}: {value}" for key, value in m["unavailable_metrics"].items()) + ".")
            if data.get("empty"):
                lines.append("Выборка пуста. Неопределённая доля не означает нулевой риск.")
        elif tool == "get_npl_trend":
            points = data["points"]
            if points:
                first, last = points[0], points[-1]
                lines.append(f"Динамика DPD 90+ по {len(points)} доступным срезам: {first['as_of']} — {last['as_of']}. "
                             f"Начальная доля: {percent(first.get('npl90_ratio'))}; конечная: {percent(last.get('npl90_ratio'))}; "
                             f"изменение: {signed_pp(data.get('npl90_delta_pp'))}.")
                lines += [f"Остаток на начальную дату: {amount(first.get('balance_rub'))}; на конечную: {amount(last.get('balance_rub'))}.",
                          "Сравнение показывает изменение доли просроченного остатка. Оно само по себе не устанавливает причины; остатки разных дат не суммируются."]
                charts.append(_chart("line", "Динамика DPD 90+", "ratio", [_series("DPD 90+", points, "as_of", "npl90_ratio")],
                                     data, ref, dataset_id, "Доля взвешена по остатку, не по количеству кредитов. Пропуск остаётся пропуском."))
                charts.append(_chart("line", "Остаток портфеля по датам", "rub", [_series("Остаток", points, "as_of", "balance_rub")],
                                     data, ref, dataset_id, "Каждая точка — отдельный срез; суммы по датам не складываются."))
            else:
                lines.append("В выбранном периоде нет доступных срезов.")
        elif tool in {"analyze_segment", "get_top_risk_segments"}:
            dim = data["dimension"]
            label = {"region": "регионов", "product": "продуктов", "risk_band": "рейтингов"}[dim]
            rows, metric = data["segments"], data.get("sort_metric", "npl90_ratio")
            metric_label = {"npl90_ratio": "DPD 90+", "balance_rub": "остатку", "npl90_balance_rub": "остатку DPD 90+", "el_ratio": "доле учебного EL"}[metric]
            unit = "rub" if metric.endswith("_rub") else "ratio"
            fmt = amount if unit == "rub" else percent
            lines.insert(0, f"Сравнение {label} по {metric_label}" + (" (рейтинг возвращённых сегментов)." if tool == "get_top_risk_segments" else "."))
            for row in rows:
                lines.append(f"{row['segment']}: DPD 90+ {percent(row.get('npl90_ratio'))}; остаток {amount(row.get('balance_rub'))}; учебный EL {amount(row.get('el_rub'))}.")
            eligible = [row for row in rows if number(row.get(metric))]
            if eligible:
                high = max(eligible, key=lambda row: row[metric])
                lines.append(f"Максимальное значение ({metric_label}): {high['segment']} — {fmt(high[metric])}.")
                if tool == "analyze_segment":
                    low = min(eligible, key=lambda row: row[metric])
                    lines.append(f"Минимальное значение ({metric_label}): {low['segment']} — {fmt(low[metric])}.")
            elif not rows:
                lines.append("Нет сегментов для выбранной области и порога числа кредитов.")
            if tool == "get_top_risk_segments":
                lines.append(f"Показан только рейтинг MCP, минимальное число кредитов: {data.get('min_loans', 30)}. Это не полный перечень сегментов; общий минимум по нему определить нельзя.")
            lines.append("Доля просроченного остатка и абсолютный размер потерь — разные показатели. Сравнение не является основанием для индивидуального кредитного решения.")
            charts.append(_chart("bar", f"Сравнение {label}: {metric_label}", unit,
                                 [_series(metric_label, rows, "segment", metric)], data, ref, dataset_id,
                                 "Только возвращённые MCP сегменты." if tool == "get_top_risk_segments" else "Все сегменты выбранной области."))
        elif tool == "get_delinquency_distribution":
            lines.insert(0, "Распределение остатка по DPD-корзинам, включая DPD 90+.")
            for row in data["buckets"]:
                lines.append(f"DPD {row['bucket']}: {percent(row.get('balance_share'))}; {amount(row.get('balance_rub'))}.")
            lines.append("Корзины взаимно исключаются; каждый кредит учитывается один раз на выбранную дату.")
            charts.append(_chart("bar", "Распределение остатка по DPD", "ratio",
                                 [_series("Доля остатка", data["buckets"], "bucket", "balance_share")], data, ref, dataset_id))
        elif tool == "run_stress_scenario":
            lines += [f"Условия: PD ×{data['pd_multiplier']:g}; LGD +{data['lgd_add_pp']:g} п.п.",
                      f"Исходный EL: {amount(data['base_el_rub'])}. Стрессовый EL: {amount(data['stressed_el_rub'])}. Изменение: {amount(data['delta_el_rub'])}.",
                      "Формула исходного EL: Σ(EAD × PD12 × LGD). Стресс: Σ(EAD × min(1, PD12 × множитель) × min(1, LGD + прибавка/100)).",
                      "What-if, не прогноз. PD и LGD ограничены единицей; EAD фиксирован. Сценарий показывает чувствительность при заданных допущениях, а не вероятность будущих потерь. Не IFRS 9 ECL."]
            series = [{"name": "Учебный EL", "points": [{"label": "Исходный", "value": data["base_el_rub"]}, {"label": "Стрессовый", "value": data["stressed_el_rub"]}]}]
            charts.append(_chart("bar", "Учебный EL: исходный и стрессовый", "rub", series, data, ref, dataset_id, "What-if, не прогноз. EAD фиксирован; PD/LGD ограничены 1."))
        elif tool == "explain_npl_change":
            lines += [f"Изменение DPD 90+: {signed_pp(data['npl90_delta_pp'])}."]
            for row in data["contributions"]:
                lines.append(f"{row['segment']}: общий вклад {signed_pp(row['total_contribution_pp'])}; внутрисегментный {signed_pp(row['within_segment_pp'])}; структурный {signed_pp(row['mix_pp'])}.")
            lines.append("Это арифметическая атрибуция, не доказательство причин. Формула: Δ(w × r) = avg(w) × Δr + avg(r) × Δw. Для причин нужны сведения о выдачах, платежах и изменениях состава.")
            charts.append(_chart("bar", "Вклады в изменение DPD 90+", "pp", [
                _series("Внутри сегмента", data["contributions"], "segment", "within_segment_pp"),
                _series("Изменение структуры", data["contributions"], "segment", "mix_pp")], data, ref, dataset_id,
                "Сумма двух компонент по всем сегментам равна изменению общей доли с точностью округления."))
        elif tool == "check_data_quality":
            lines += [f"Проверено записей: {data['snapshots']}; кредитов: {data['unique_loans']}; срезов: {data['months']}.",
                      f"Ошибок диапазонов: {data['invalid_rows']}; потерянных связей: {data['orphan_rows']}; SQLite: {data['sqlite_integrity']}.",
                      "Пропуски: " + "; ".join(f"{key}={value}" for key, value in data.get("missing_values", {}).items()) + ".",
                      "Проверка относится ко всему набору, а не только к фильтрам. Целостность не доказывает реалистичность PD/LGD."]
        else:
            continue
        sections.append("\n".join(f"{line} [{ref}]" for line in lines) + f"\nИсточник MCP: [{ref}].")
    return "\n\n".join(sections), charts, last_scope
