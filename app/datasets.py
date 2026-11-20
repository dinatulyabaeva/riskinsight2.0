"""Server-owned dataset registry. IDs never encode caller-supplied file paths."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

DATASET_PATTERN = r"^(builtin|upload_[0-9a-f]{32})$"


class DatasetStore:
    def __init__(self, builtin: Path):
        self.builtin = Path(builtin).resolve()
        self.root = self.builtin.parent / "imports"
        self.catalog = self.root / "catalog.sqlite"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.catalog)) as con, con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    created_at TEXT NOT NULL, row_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS imports (
                    import_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, report TEXT NOT NULL);
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(datasets)")}
            if "source_sha256" not in columns:
                con.execute("ALTER TABLE datasets ADD COLUMN source_sha256 TEXT")
            if "synthetic" not in columns:
                con.execute("ALTER TABLE datasets ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0")

    def resolve(self, dataset_id: str) -> Path:
        if not isinstance(dataset_id, str) or not re.fullmatch(DATASET_PATTERN, dataset_id):
            raise ValueError("Некорректный идентификатор набора данных")
        if dataset_id == "builtin":
            return self.builtin
        if not self.catalog.exists():
            raise ValueError("Набор данных не найден")
        with closing(sqlite3.connect(self.catalog)) as con, con:
            found = con.execute("SELECT 1 FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        path = self.root / (dataset_id + ".sqlite")
        if not found or not path.is_file() or path.is_symlink():
            raise ValueError("Набор данных не найден")
        return path

    def list(self):
        with closing(sqlite3.connect(self.catalog)) as con, con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("SELECT * FROM datasets ORDER BY created_at DESC")]
        return [{"dataset_id": "builtin", "name": "Встроенный синтетический портфель", "synthetic": True},
                *[{**r, "synthetic": bool(r.get("synthetic", False))} for r in rows]]

    def find_source(self, source_sha256):
        """Find an unchanged imported fixture; no data reads bypass MCP."""
        with closing(sqlite3.connect(self.catalog)) as con, con:
            rows = con.execute("SELECT dataset_id FROM datasets WHERE source_sha256=? ORDER BY created_at",
                               (source_sha256,)).fetchall()
        for row in rows:
            try:
                self.resolve(row[0])
                return row[0]
            except ValueError:
                continue
        return None

    def record(self, report, name=None):
        report = {**report, "import_id": uuid.uuid4().hex,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        with closing(sqlite3.connect(self.catalog)) as con, con:
            if report["status"] == "imported":
                con.execute("INSERT INTO datasets (dataset_id,name,created_at,row_count,source_sha256,synthetic) VALUES (?,?,?,?,?,?)",
                            (report["dataset_id"], name, report["created_at"], report["row_count"],
                             report.get("source_sha256"), int(report.get("synthetic", False))))
            con.execute("INSERT INTO imports VALUES (?,?,?)",
                        (report["import_id"], report["created_at"], json.dumps(report, ensure_ascii=False)))
        return report

    def history(self):
        with closing(sqlite3.connect(self.catalog)) as con, con:
            rows = con.execute("SELECT report FROM imports ORDER BY created_at DESC LIMIT 50").fetchall()
        return [json.loads(r[0]) for r in rows]
