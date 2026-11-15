"""RiskInsight real-LLM evaluation; run: python -m scripts.eval_riskinsight (20 cases by default)"""
import argparse
import asyncio
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from app.agent import ChatRequest, run_agent
from app.config import Settings

CASES = [
 ('overview', 'Назови остаток портфеля и долю DPD 90+ на последнем срезе, числитель, знаменатель и источник.', 'get_portfolio_summary'),
 ('overview_alt', 'Каков общий объём задолженности и процент просрочки 90+ сейчас?', 'get_portfolio_summary'),
 ('trend', 'Как изменилась доля DPD 90+ за последние шесть доступных месяцев? Укажи даты, изменение в п.п. и источник.', 'get_npl_trend'),
 ('trend_alt', 'Покажи тренд NPL 90 за 6 месяцев с первой и последней точкой.', 'get_npl_trend'),
 ('products', 'Какие три продукта имеют самую высокую долю DPD 90+? Назови доли, остатки и источники.', 'analyze_segment'),
 ('products_alt', 'Топ-3 продуктов по риску просрочки DPD 90+ с остатками.', 'analyze_segment'),
 ('stress', 'Что будет с EL, если PD увеличить в 1,5 раза, а LGD на 5 процентных пунктов? Укажи исходный и стрессовый EL.', 'run_stress_scenario'),
 ('stress_alt', 'Оцени стресс: PD ×1,5 и LGD +5 п.п. Насколько изменятся ожидаемые потери?', 'run_stress_scenario'),
 ('quality', 'Проверь качество данных: сколько записей и есть ли ошибки целостности?', 'check_data_quality'),
 ('quality_alt', 'Есть ли проблемы качества и целостности в данных кредитного портфеля?', 'check_data_quality'),
 ('regions', 'Какие регионы имеют максимальную долю DPD 90+? Укажи источники.', 'get_top_risk_segments'),
 ('regions_alt', 'Сравни риск просрочки по регионам на последнюю дату.', 'analyze_segment'),
 ('distribution', 'Как распределён остаток по корзинам просрочки?', 'get_delinquency_distribution'),
 ('distribution_alt', 'Покажи доли задолженности по DPD-бакетам.', 'get_delinquency_distribution'),
 ('drivers', 'За счёт чего изменилась доля DPD 90+ за последние шесть месяцев по продуктам?', 'explain_npl_change'),
 ('drivers_alt', 'Разложи изменение NPL 90 по продуктам на структурный и внутрисегментный вклад.', 'explain_npl_change'),
 ('segment', 'Сравни продукты по доле просроченного остатка на последнем срезе.', 'analyze_segment'),
 ('segment_alt', 'Как различается DPD 90+ между кредитными продуктами?', 'analyze_segment'),
 ('methodology', 'Какая формула используется для доли DPD 90+? Укажи источник данных.', 'get_dataset_context'),
 ('limitations', 'Чем учебный EL отличается от IFRS 9 ECL? Укажи источник методологии.', 'get_dataset_context'),
]

async def evaluate(case, cfg, dataset_id):
    case_id, question, expected = case
    start = time.perf_counter()
    row = {'id': case_id, 'question': question, 'expected_tool': expected, 'status': 'error', 'seconds': None,
           'llm_requests': None, 'actual_tools': '', 'tool_ok': False, 'has_citation': False,
           'citation_valid': False, 'mode_of_answer': '', 'error': '', 'answer': ''}
    try:
        result = await run_agent(ChatRequest(dataset_id=dataset_id, question=question), cfg)
        trace = result.get('trace', [])
        successful = [t for t in trace if t.get('ok')]
        refs = {t.get('ref') for t in successful}
        answer = result.get('answer', '')
        cited = set(re.findall(r'\[(T\d+)\]', answer))
        usage = result.get('usage', {})
        row.update(status='completed', llm_requests=usage.get('llm_requests', 0),
                   actual_tools=', '.join(t.get('tool', '') + (' ✓' if t.get('ok') else ' ✗') for t in trace),
                   tool_ok=any(t.get('tool') == expected and t.get('ok') for t in trace),
                   has_citation=bool(cited), citation_valid=bool(cited) and cited.issubset(refs),
                   mode_of_answer='deterministic' if usage.get('llm_requests', 0) == 0 else 'llm_involved',
                   answer=answer)
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {str(exc)[:500]}'
        trace = getattr(exc, 'mcp_trace', [])
        if isinstance(trace, list):
            row['actual_tools'] = ', '.join(
                str(t.get('tool', '')) + (' ✓' if t.get('ok') else ' ✗')
                for t in trace if isinstance(t, dict)
            )
            row['tool_ok'] = any(
                isinstance(t, dict) and t.get('tool') == expected and t.get('ok')
                for t in trace
            )
    row['seconds'] = round(time.perf_counter() - start, 2)
    return row

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=len(CASES), help='Number of cases, max 20; default all 20')
    parser.add_argument('--dataset-id', default='builtin')
    args = parser.parse_args()
    cfg = Settings.from_env()
    if cfg.mode != 'llm' or not cfg.llm_configured:
        raise SystemExit('Set APP_MODE=llm and configure the model first')
    cases = CASES[:max(1, min(20, args.limit))]
    folder = Path('eval_results')
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    rows = []
    for i, case in enumerate(cases, 1):
        print(f'[{i}/{len(cases)}] {case[0]}...', flush=True)
        row = await evaluate(case, cfg, args.dataset_id)
        rows.append(row)
        print(f"  {row['status']} | tool={row['tool_ok']} | cite={row['citation_valid']} | mode={row['mode_of_answer'] or 'unknown'} | {row['seconds']}s", flush=True)
        # Persist after each case, including failures and partial runs.
        (folder / f'eval_{stamp}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        with (folder / f'eval_{stamp}.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print('--- SUMMARY ---')
    print(f"Completed: {sum(r['status']=='completed' for r in rows)}/{len(rows)}")
    print(f"Expected MCP tool successful: {sum(r['tool_ok'] for r in rows)}/{len(rows)}")
    print(f"Valid MCP citations: {sum(r['citation_valid'] for r in rows)}/{len(rows)}")
    print(f"Deterministic completed: {sum(r['status']=='completed' and r['mode_of_answer']=='deterministic' for r in rows)}/{len(rows)}")
    print(f"LLM-involved completed: {sum(r['status']=='completed' and r['mode_of_answer']=='llm_involved' for r in rows)}/{len(rows)}")
    print('NOTE: llm_involved means the model was called; it does NOT prove the final answer was model-written.')
    print('NOTE: On failed runs, MCP success is recorded when the AgentError contains a trace.')
    print('Numeric accuracy: NOT AUTOMATICALLY VERIFIED; compare answers with MCP results manually.')
    print(f'Reports: {folder / f"eval_{stamp}.csv"} and {folder / f"eval_{stamp}.json"}')

if __name__ == '__main__':
    asyncio.run(main())
