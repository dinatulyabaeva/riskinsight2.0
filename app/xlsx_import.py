"""Bounded XLSX parser and atomic write path, separate from read-only MCP analytics."""
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import os
import sqlite3
import tempfile
import uuid
from zipfile import ZipFile
from defusedxml.ElementTree import iterparse
from openpyxl import load_workbook
from .datasets import DatasetStore

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ROWS = 100_000
MAX_ERRORS = 100
COLUMNS = ("loan_id", "as_of", "product", "region", "risk_band", "balance_rub", "dpd", "pd_12m", "lgd")
REQUIRED = {"loan_id", "as_of", "product", "balance_rub", "dpd"}
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE loans (loan_id TEXT PRIMARY KEY);
CREATE TABLE snapshots (
 as_of TEXT NOT NULL, loan_id TEXT NOT NULL REFERENCES loans(loan_id),
 product TEXT NOT NULL, region TEXT, risk_band TEXT,
 balance_kop INTEGER NOT NULL CHECK(balance_kop>=0), dpd INTEGER NOT NULL CHECK(dpd BETWEEN 0 AND 36500),
 pd_12m REAL CHECK(pd_12m BETWEEN 0 AND 1), lgd REAL CHECK(lgd BETWEEN 0 AND 1), annual_rate REAL,
 PRIMARY KEY(as_of,loan_id));
CREATE INDEX idx_snapshots_loan ON snapshots(loan_id);
"""


def rejected(message, code="invalid_file"):
    return {"status": "rejected", "dataset_id": None, "row_count": 0, "valid_rows": 0,
            "invalid_rows": 0, "error_count": 1, "errors_truncated": False,
            "errors": [{"row": None, "column": None, "code": code, "message": message}], "warnings": []}


def _text(value, column):
    if column == "loan_id" and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > (128 if column == "loan_id" else 80):
        raise ValueError("Ожидается непустой текст допустимой длины")
    value = value.strip()
    if any(ord(c) < 32 for c in value):
        raise ValueError("Управляющие символы запрещены")
    if column == "risk_band" and value not in {"A", "B", "C", "D"}:
        raise ValueError("risk_band должен быть A, B, C или D")
    return value


def _number(value, column):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Ожидается числовая ячейка, не текст")
    try:
        n = Decimal(str(value))
        upper = Decimal("100000000000") if column == "balance_rub" else Decimal(36500 if column == "dpd" else 1)
        if not n.is_finite() or not 0 <= n <= upper:
            raise ValueError(f"Допустимый диапазон: 0..{upper}")
        if column == "balance_rub":
            if n * 100 != (n * 100).to_integral_value():
                raise ValueError("Остаток должен содержать не более двух знаков после запятой")
            return int(n * 100)
        if column == "dpd":
            if n != n.to_integral_value():
                raise ValueError("DPD должен быть целым числом")
            return int(n)
        return float(n)
    except InvalidOperation:
        raise ValueError("Некорректное число") from None


def _date(value):
    if isinstance(value, datetime):
        if value.time().isoformat() != "00:00:00":
            raise ValueError("Дата среза не должна содержать время")
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if value == parsed.isoformat():
                return value
        except ValueError:
            pass
    raise ValueError("Ожидается дата Excel или корректная дата YYYY-MM-DD")


def validate_xlsx(content: bytes):
    """Never evaluate formulas; reject formulas, active content, external links and XML entities."""
    if len(content) > MAX_UPLOAD_BYTES:
        return rejected("Размер XLSX превышает 10 МБ", "file_too_large"), []
    workbook = None
    try:
        with ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(x.file_size for x in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Слишком большой распакованный XLSX")
            if len({x.filename for x in entries}) != len(entries):
                raise ValueError("Повторяющиеся части XLSX")
            for entry in entries:
                name = entry.filename.lower()
                if entry.flag_bits & 1 or any(s in name for s in ("vbaproject", "externallinks/", "embeddings/", "macrosheets/", "activex/")):
                    raise ValueError("Макросы, внешние ссылки, шифрование и встроенные объекты запрещены")
                if name.endswith((".xml", ".rels")):
                    with archive.open(entry) as stream:
                        for _, node in iterparse(stream, events=("end",), forbid_dtd=True):
                            if node.tag.endswith("Relationship") and node.attrib.get("TargetMode") == "External":
                                raise ValueError("Внешние связи XLSX запрещены")
                            if node.tag.endswith("Override") and "macroenabled" in node.attrib.get("ContentType", "").lower():
                                raise ValueError("Макросы запрещены")
                            node.clear()
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        if len(workbook.worksheets) != 1:
            raise ValueError("Ожидается ровно один лист с таблицей портфеля")
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()  # Do not trust attacker-controlled worksheet dimensions.
        rows = sheet.iter_rows()
        header = next(rows, ())
        if not header or len(header) > len(COLUMNS):
            raise ValueError("Некорректный заголовок таблицы")
        names = [c.value for c in header]
        if any(not isinstance(n, str) or n not in COLUMNS for n in names) or len(set(names)) != len(names):
            raise ValueError("Неизвестные или повторяющиеся колонки; используйте заголовки из шаблона")
        if REQUIRED - set(names):
            raise ValueError("Отсутствуют обязательные колонки: " + ", ".join(sorted(REQUIRED - set(names))))
        errors, records, seen, nulls = [], [], set(), {k: 0 for k in COLUMNS if k not in REQUIRED}
        count = invalid = error_count = 0
        def add_error(row, column, code, message):
            nonlocal error_count
            error_count += 1
            if len(errors) < MAX_ERRORS:
                errors.append({"row": row, "column": column, "code": code, "message": message})
        for row_number, cells in enumerate(rows, 2):
            if all(c.value is None for c in cells):
                continue
            count += 1
            if count > MAX_ROWS:
                raise ValueError(f"Допускается не более {MAX_ROWS} строк")
            before = error_count
            record = {k: None for k in COLUMNS}
            if len(cells) > len(names) and any(c.value is not None for c in cells[len(names):]):
                add_error(row_number, None, "extra_cells", "Ячейки за пределами заголовка")
            for index, column in enumerate(names):
                cell = cells[index] if index < len(cells) else None
                value = cell.value if cell else None
                if cell and cell.data_type in {"f", "e"}:
                    add_error(row_number, column, "formula_or_error", "Формулы и ошибки Excel запрещены")
                    continue
                if value is None or isinstance(value, str) and not value.strip():
                    if column in REQUIRED:
                        add_error(row_number, column, "missing_value", "Обязательное значение отсутствует")
                    else:
                        nulls[column] += 1
                    continue
                try:
                    record[column] = (_date(value) if column == "as_of" else
                                      _number(value, column) if column in {"balance_rub", "dpd", "pd_12m", "lgd"} else
                                      _text(value, column))
                except ValueError as exc:
                    add_error(row_number, column, "invalid_value", str(exc))
            key = (record["as_of"], record["loan_id"])
            if all(key):
                if key in seen:
                    add_error(row_number, "loan_id", "duplicate", "Дубликат пары (loan_id, as_of)")
                seen.add(key)
            if error_count == before:
                records.append(record)
            else:
                invalid += 1
        if not count:
            raise ValueError("Таблица не содержит строк данных")
        warnings = [f"Колонка {k} отсутствует" for k in COLUMNS if k not in names]
        warnings += [f"{k}: пропущено значений — {n}" for k, n in nulls.items() if n]
        return {"status": "rejected" if error_count else "validated", "dataset_id": None,
                "row_count": count, "valid_rows": count - invalid, "invalid_rows": invalid,
                "error_count": error_count, "errors": errors, "errors_truncated": error_count > len(errors),
                "warnings": warnings}, records
    except Exception as exc:
        # Do not echo parser internals, cell contents, archive paths or uploaded code.
        message = str(exc) if type(exc) is ValueError else "Не удалось прочитать XLSX: файл поврежден или имеет недопустимую структуру"
        return rejected(message), []
    finally:
        if workbook:
            workbook.close()


def import_xlsx(store: DatasetStore, filename: str, content: bytes):
    if not filename.lower().endswith(".xlsx"):
        return store.record(rejected("Допускаются только файлы .xlsx", "invalid_extension"))
    report, records = validate_xlsx(content)
    if report["status"] == "rejected":
        return store.record(report)
    dataset_id = "upload_" + uuid.uuid4().hex
    path = store.root / (dataset_id + ".sqlite")
    fd, temporary = tempfile.mkstemp(prefix="import-", suffix=".sqlite", dir=store.root)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as con, con:
            con.executescript(SCHEMA)
            con.executemany("INSERT INTO loans VALUES (?)", [(k,) for k in sorted({r["loan_id"] for r in records})])
            con.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                            [(r["as_of"], r["loan_id"], r["product"], r["region"], r["risk_band"],
                              r["balance_rub"], r["dpd"], r["pd_12m"], r["lgd"]) for r in records])
            con.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("dataset_version", dataset_id), ("synthetic", "false"), ("schema", "xlsx-v1"),
                ("row_count", str(len(records))), ("currency", "RUB")])
        os.replace(temporary, path)  # All SQLite handles are closed before rename, including on Windows.
        try:
            name = filename.replace("\\", "/").rsplit("/", 1)[-1][:128]
            return store.record({**report, "status": "imported", "dataset_id": dataset_id}, name)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
