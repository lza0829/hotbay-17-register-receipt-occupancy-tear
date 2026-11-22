"""测试环境：独立临时库，先于任何 app 模块导入。"""
import os
import pathlib
import tempfile

_TMP = tempfile.mkdtemp(prefix="hotbay-test-")
os.environ["HOTBAY_DB"] = str(pathlib.Path(_TMP) / "test.db")

import pytest  # noqa: E402

from app.db import db, init_db  # noqa: E402

TABLES = [
    "register_audits", "verify_runs", "inventory_objects", "destroy_runs",
    "migrate_jobs", "clips", "partitions", "retention_policies", "settings", "vaults",
]


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    with db() as conn:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("INSERT INTO vaults(name, capacity, active) VALUES ('测试间A', 2, 1)")
        conn.execute("INSERT INTO vaults(name, capacity, active) VALUES ('测试间B', 1, 1)")
    yield


def vault_id(name: str) -> int:
    with db() as conn:
        return conn.execute("SELECT id FROM vaults WHERE name=?", (name,)).fetchone()["id"]
