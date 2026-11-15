"""Create small fictitious XLSX portfolios for manual acceptance, without real client data."""
import argparse
from pathlib import Path
from openpyxl import Workbook
from app.config import ROOT


def write_example(path, rows, headers):
    workbook = Workbook()
    workbook.active.append(headers)
    for row in rows:
        workbook.active.append(row)
    workbook.save(path)
    workbook.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'examples')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    headers = ['loan_id', 'as_of', 'product', 'balance_rub', 'dpd']
    rows = [['EXAMPLE-1', '2026-10-31', 'Пример продукта', 1000, 0],
            ['EXAMPLE-2', '2026-10-31', 'Пример продукта', 3000, 90]]
    write_example(args.output / 'portfolio.xlsx', rows, headers)
    write_example(args.output / 'portfolio-invalid.xlsx', rows + [rows[0], ['EXAMPLE-3', '2026-02-30', 'Пример продукта', -100, 1]], headers)
    write_example(args.output / 'portfolio-full.xlsx', [rows[0]+['Север', 'A', .1, .5], rows[1]+['Юг', 'B', .2, .4]],
                  headers+['region', 'risk_band', 'pd_12m', 'lgd'])
    print('Created portfolio.xlsx (DPD90+ = 3000/4000 = 75%), portfolio-full.xlsx (EL = 290 RUB), portfolio-invalid.xlsx')


if __name__ == '__main__':
    main()
