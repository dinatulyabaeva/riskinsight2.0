"""XLSX validation, immutable imports and security boundaries, without Excel/OS-specific APIs."""
from contextlib import closing
from datetime import datetime
from io import BytesIO
import hashlib
import sqlite3
from zipfile import ZipFile, ZIP_DEFLATED
import pytest
from openpyxl import Workbook
from app.datasets import DatasetStore
from app.xlsx_import import COLUMNS, MAX_UPLOAD_BYTES, import_xlsx, validate_xlsx
from app.analytics import Analytics

HEADERS = ["loan_id", "as_of", "product", "balance_rub", "dpd"]
ROWS = [["X1", "2026-10-31", "Загруженный продукт", 1000, 0],
        ["X2", "2026-10-31", "Загруженный продукт", 3000, 90]]


def xlsx(rows=None, headers=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers if headers is not None else HEADERS)
    for row in ROWS if rows is None else rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


@pytest.fixture
def store(tiny_db):
    result = DatasetStore(tiny_db)
    result.initialize()
    return result


def test_minimal_format_counts_missing_metrics():
    report, records = validate_xlsx(xlsx())
    assert report["status"] == "validated"
    assert report["row_count"] == report["valid_rows"] == 2
    assert report["invalid_rows"] == report["error_count"] == 0
    assert len(report["warnings"]) == 4
    assert records[1]["balance_rub"] == 300000  # normalized to integer kopeks
    assert records[0]["pd_12m"] is None


@pytest.mark.parametrize("column,value", [
    ("loan_id", None), ("loan_id", True), ("loan_id", " "), ("loan_id", "x" * 129),
    ("product", None), ("product", 123), ("product", "x\nSQL"),
    ("as_of", "2026-02-30"), ("as_of", "31.10.2026"), ("as_of", 1234),
    ("as_of", datetime(2026, 10, 31, 12)), ("as_of", "20261031"),
    ("balance_rub", -1), ("balance_rub", 0.001), ("balance_rub", "100"),
    ("balance_rub", True), ("balance_rub", 100000000001),
    ("dpd", -1), ("dpd", 2.5), ("dpd", 36501), ("dpd", "90"),
    ("risk_band", "Z"), ("pd_12m", -0.1), ("pd_12m", 1.1), ("lgd", 50),
    ("pd_12m", "0.2"), ("region", 5), ("lgd", "=1+1"), ("balance_rub", "=1000+1"),
    ("balance_rub", "#DIV/0!"),
])
def test_invalid_values_have_cell_locations(column, value):
    values = {**dict(zip(HEADERS, ROWS[0])), "region": "Север", "risk_band": "A", "pd_12m": .1, "lgd": .5}
    values[column] = value
    report, _ = validate_xlsx(xlsx([[values[k] for k in COLUMNS]], list(COLUMNS)))
    assert report["status"] == "rejected"
    assert report["invalid_rows"] == 1
    assert report["errors"][0]["row"] == 2
    assert report["errors"][0]["column"] == column
    assert str(value) not in str(report["errors"]) if isinstance(value, str) and len(value) > 100 else True


def test_dates_zero_balances_and_numeric_loan_ids():
    report, records = validate_xlsx(xlsx([[123, datetime(2026, 10, 31), "P", 0, 0]]))
    assert report["status"] == "validated"
    assert records[0]["loan_id"] == "123"
    assert records[0]["as_of"] == "2026-10-31"


@pytest.mark.parametrize("headers", [["loan_id", "as_of"], HEADERS + ["sql"], HEADERS + ["dpd"]])
def test_bad_headers(headers):
    assert validate_xlsx(xlsx(headers=headers))[0]["status"] == "rejected"


def test_empty_and_corrupt_files():
    for content in (b"not an Excel file", b"", xlsx(rows=[])):
        assert validate_xlsx(content)[0]["status"] == "rejected"


def test_duplicates_are_snapshot_keys_and_all_errors_counted():
    report, _ = validate_xlsx(xlsx([ROWS[0], ROWS[0], [*ROWS[0][:3], -1, -1]]))
    assert report["row_count"] == 3
    assert report["valid_rows"] == 1 and report["invalid_rows"] == 2
    assert report["error_count"] == 4
    assert any(e["code"] == "duplicate" for e in report["errors"])
    valid = [ROWS[0], ["X1", "2026-11-30", "Другой продукт", 500, 90]]
    assert validate_xlsx(xlsx(valid))[0]["status"] == "validated"


def test_error_output_bounded():
    report, _ = validate_xlsx(xlsx([[f"X{i}", "bad-date", "P", -1, 0] for i in range(110)]))
    assert report["error_count"] == 220 and len(report["errors"]) == 100
    assert report["errors_truncated"]


def rewrite_zip(content, extra=None, replace=None):
    out = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(out, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, (replace or {}).get(name, source.read(name)))
        for name, value in (extra or {}).items():
            target.writestr(name, value)
    return out.getvalue()


@pytest.mark.parametrize("name", ["xl/vbaProject.bin", "xl/externalLinks/externalLink1.xml", "xl/embeddings/ole.bin", "xl/activeX/control.bin"])
def test_active_content_rejected(name):
    report, _ = validate_xlsx(rewrite_zip(xlsx(), extra={name: b"payload"}))
    assert report["status"] == "rejected"


def test_xml_entities_and_external_relationships_rejected():
    payloads = [b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
                b'<Relationships><Relationship TargetMode="External" Target="https://example.com"/></Relationships>']
    for payload in payloads:
        assert validate_xlsx(rewrite_zip(xlsx(), extra={"xl/untrusted.xml": payload}))[0]["status"] == "rejected"


def test_expansion_and_row_limits(monkeypatch):
    monkeypatch.setattr("app.xlsx_import.MAX_EXPANDED_BYTES", 100)
    assert validate_xlsx(xlsx())[0]["status"] == "rejected"
    monkeypatch.setattr("app.xlsx_import.MAX_EXPANDED_BYTES", 100_000_000)
    monkeypatch.setattr("app.xlsx_import.MAX_ROWS", 1)
    assert validate_xlsx(xlsx())[0]["status"] == "rejected"


def test_multiple_sheets_rejected():
    workbook = Workbook()
    workbook.create_sheet("Extra")
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    assert validate_xlsx(buffer.getvalue())[0]["status"] == "rejected"


def test_import_atomic_immutable_and_persistent(store):
    before = hashlib.sha256(store.builtin.read_bytes()).hexdigest()
    first = import_xlsx(store, "portfolio.xlsx", xlsx())
    second = import_xlsx(store, "portfolio.xlsx", xlsx())
    assert first["status"] == second["status"] == "imported"
    assert first["dataset_id"] != second["dataset_id"]
    reopened = DatasetStore(store.builtin)
    assert len(reopened.list()) == 3
    path = reopened.resolve(first["dataset_id"])
    assert hashlib.sha256(store.builtin.read_bytes()).hexdigest() == before
    summary = Analytics(path, first["dataset_id"]).summary()
    assert not summary["synthetic"]
    assert summary["metrics"]["npl90_ratio"] == .75
    assert summary["metrics"]["el_rub"] is None
    assert summary["metrics"]["weighted_rate"] is None
    assert summary["metrics"]["unavailable_metrics"]
    calc = summary["npl90_calculation"]
    assert calc["numerator_rub"] == 3000 and calc["denominator_rub"] == 4000
    assert calc["formula"] == "SUM(balance_kop WHERE dpd >= 90) / SUM(balance_kop)"
    assert calc["as_of"] == "2026-10-31" and calc["dataset_id"] == first["dataset_id"]
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT typeof(balance_kop) FROM snapshots LIMIT 1").fetchone()[0] == "integer"
    with pytest.raises(ValueError, match="PD/LGD"):
        Analytics(path, first["dataset_id"]).stress()


def test_partial_optional_values_do_not_understate_el(store):
    headers = HEADERS + ["pd_12m", "lgd", "region"]
    report = import_xlsx(store, "partial.xlsx", xlsx([ROWS[0]+[.1, .5, "Север"], ROWS[1]+[None, .5, None]], headers))
    analytics = Analytics(store.resolve(report["dataset_id"]), report["dataset_id"])
    assert analytics.summary()["metrics"]["el_rub"] is None
    assert analytics.summary(region="Север")["metrics"]["el_rub"] == 50
    assert analytics.stress(1, 0, region="Север")["base_el_rub"] == 50
    assert analytics.context()["missing_values"]["pd_12m"] == 1


def test_full_import_calculations_and_changing_snapshot_dimensions(store):
    rows = [["A", "2026-09-30", "P1", 100.01, 89, .1, .5],
            ["B", "2026-09-30", "P2", 300.02, 90, .2, .4],
            ["A", "2026-10-31", "P2", 100.01, 90, .3, .5],
            ["B", "2026-10-31", "P2", 300.02, 0, .2, .4]]
    r = import_xlsx(store, "full.xlsx", xlsx(rows, HEADERS + ["pd_12m", "lgd"]))
    a = Analytics(store.resolve(r["dataset_id"]), r["dataset_id"])
    assert a.summary()["metrics"]["balance_rub"] == 400.03
    assert a.summary()["metrics"]["el_rub"] == 39
    assert a.summary()["metrics"]["npl90_ratio"] == pytest.approx(100.01 / 400.03)
    assert a.summary(product="P1")["empty"]
    assert a.summary("2026-09-30", product="P1")["metrics"]["balance_rub"] == 100.01
    assert len(a.trend()["points"]) == 2
    assert sum(b["balance_rub"] for b in a.delinquency()["buckets"]) == 400.03
    drivers = a.drivers("2026-09-30", "2026-10-31")
    assert sum(r["total_contribution_pp"] for r in drivers["contributions"]) == pytest.approx(drivers["npl90_delta_pp"], abs=2e-6)
    assert a.quality()["passed"]


def test_zero_denominator_is_null_and_drivers_do_not_divide_by_zero(store):
    rows = [["A", "2026-09-30", "P", 0, 90], ["A", "2026-10-31", "P", 0, 0]]
    report = import_xlsx(store, "zero.xlsx", xlsx(rows))
    a = Analytics(store.resolve(report["dataset_id"]), report["dataset_id"])
    assert a.summary()["metrics"]["npl90_ratio"] is None
    assert a.summary()["npl90_calculation"]["denominator_rub"] == 0
    with pytest.raises(ValueError, match="Недостаточно"):
        a.drivers("2026-09-30", "2026-10-31")


def test_rejected_import_creates_only_history(store):
    r = import_xlsx(store, "bad.xlsx", xlsx([ROWS[0], ROWS[0]]))
    assert r["dataset_id"] is None and len(store.list()) == 1
    assert store.history()[0]["error_count"] == 1
    assert not list(store.root.glob("upload_*.sqlite"))


def test_failure_leaves_no_registered_dataset(store, monkeypatch):
    monkeypatch.setattr("app.xlsx_import.os.replace", lambda *a: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        import_xlsx(store, "portfolio.xlsx", xlsx())
    assert len(store.list()) == 1
    assert not list(store.root.glob("import-*.sqlite"))


@pytest.mark.parametrize("identifier", ["../portfolio.sqlite", "/etc/passwd", "C:\\data\\portfolio.sqlite", "file:///tmp/a", "upload_"+"a"*32])
def test_registry_rejects_paths_and_unknown_ids(store, identifier):
    with pytest.raises(ValueError):
        store.resolve(identifier)


def test_upload_size_and_extension(store):
    assert import_xlsx(store, "macros.xlsm", xlsx())["status"] == "rejected"
    report = import_xlsx(store, "large.xlsx", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert report["errors"][0]["code"] == "file_too_large"
