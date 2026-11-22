"""假登记收口（臂A：占用∪册面∪目录键可重放为准）回归测试。

夹具对应运维观察到的四类撕裂：
1. 单号绿占用未加；
2. 册有行目录无键；
3. 流水成功容量页仍满；
4. 并发双登半截。

口径：成功灯只能由「分区册行 ∪ 素材段 ∪ 湖对象键 ∪ 占用谓词」四侧
在同一事务内同命运重放出来；任一侧不成立整笔回滚并回退流水成功行。
"""
import concurrent.futures as cf

import pytest
from fastapi import HTTPException

from app.db import db
from app.services import partitions as svc
from app.services import register_evidence
from app.services.core import tier_occupied_counts, vault_partition_count
from conftest import vault_id


def clips_of(*keys):
    return [{"object_key": k, "bytes": 100} for k in keys]


def audit_rows():
    return svc.list_register_audits()


# ---------------------------------------------------------------- 夹具 1
def test_green_receipt_requires_occupancy_increment():
    """单号绿但占用未加：登记回执的「占用后」必须等于册页实际在册数，
    且必须有册行/段/目录键三侧证据，不能只靠回执文案。"""
    vid = vault_id("测试间A")
    r = svc.create_partition("P-绿", "片", "2026-01-01", vid, "热档", "reel/g/",
                             clips_of("reel/g/1.bin"))
    with db() as conn:
        used = vault_partition_count(conn, vid)
        ev = register_evidence.partition_evidence(conn, "P-绿")
        row = conn.execute("SELECT 1 FROM partitions WHERE part_key='P-绿'").fetchone()
        clips = conn.execute("SELECT COUNT(*) AS c FROM clips WHERE object_key='reel/g/1.bin'").fetchone()["c"]
        inv = conn.execute("SELECT COUNT(*) AS c FROM inventory_objects WHERE object_key='reel/g/1.bin'").fetchone()["c"]
    assert r["占用后"] == used == 1
    assert row and clips == 1 and inv == 1
    assert ev["可重放"] and r["证据"]["可重放"]
    assert r["register_ok"] is True and r["ok"] is True


# ---------------------------------------------------------------- 夹具 2
def test_partition_row_without_catalog_key_is_not_green():
    """册有行目录无键：成功流水当場回退，总览/容量页暴露缺口，不再亮绿。"""
    vid = vault_id("测试间A")
    r = svc.create_partition("P-缺键", "片", "2026-01-01", vid, "热档", "reel/miss/",
                             clips_of("reel/miss/1.bin"))
    # 模拟目录键被旁路拆除（如「只删元数据」误删目录 / 手工删行）
    with db() as conn:
        conn.execute("DELETE FROM inventory_objects WHERE object_key='reel/miss/1.bin'")

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    audits = c.get("/api/登记审计").json()
    hit = next(a for a in audits if a["id"] == r["audit_id"])
    assert hit["result"] == "回退"
    assert "目录无键" in hit["缺口"]

    ov = c.get("/api/总览").json()
    assert ov["登记缺口"] == 1
    assert ov["缺口明细"][0]["part_key"] == "P-缺键"

    vaults = {v["id"]: v for v in c.get("/api/片库间").json()}
    assert vaults[vid]["登记缺口"] == 1

    parts = c.get("/api/分区").json()
    hit_p = next(p for p in parts if p["part_key"] == "P-缺键")
    assert hit_p["证据可重放"] is False and "目录无键" in hit_p["证据缺口"]


# ---------------------------------------------------------------- 夹具 3
def test_success_audit_never_outlives_full_capacity_evidence():
    """流水成功容量页仍满：满容库不得写出成功册行；占用以册页为准。"""
    vid = vault_id("测试间B")  # 额定 1
    svc.create_partition("P-占满", "片", "2026-01-01", vid, "热档", "reel/f/",
                         clips_of("reel/f/1.bin"))
    with pytest.raises(HTTPException):
        svc.create_partition("P-溢出", "片", "2026-01-01", vid, "热档", "reel/f/",
                             clips_of("reel/f/2.bin"))
    with db() as conn:
        assert vault_partition_count(conn, vid) == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM partitions").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM inventory_objects").fetchone()["c"] == 1
    # 失败流水不留成功，成功流水仅一笔
    results = [a["result"] for a in audit_rows()]
    assert results.count("成功") == 1
    assert results.count("失败") == 1


# ---------------------------------------------------------------- 夹具 4
def test_concurrent_dual_register_never_one_green_one_stuck():
    """并发双登不得长期一绿一僵：成功者证据齐全，失败者账实两清。"""
    vid = vault_id("测试间B")  # 额定 1

    def attempt(i):
        try:
            svc.create_partition(f"P-并{i}", "片", "2026-01-01", vid, "热档", "reel/p/",
                                 clips_of(f"reel/p/{i}.bin"))
            return "成功"
        except HTTPException:
            return "失败"

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(attempt, range(1, 7)))
    assert results.count("成功") == 1

    with db() as conn:
        assert vault_partition_count(conn, vid) == 1
        ev_ok = [
            register_evidence.partition_evidence(conn, f"P-并{i}")["可重放"]
            for i in range(1, 7)
        ]
        parts = conn.execute("SELECT COUNT(*) AS c FROM partitions").fetchone()["c"]
        clips = conn.execute("SELECT COUNT(*) AS c FROM clips").fetchone()["c"]
        inv = conn.execute("SELECT COUNT(*) AS c FROM inventory_objects").fetchone()["c"]
    # 唯一占住的名额证据完整，其余不存在的册行自然不可重放
    assert sum(ev_ok) == 1
    assert parts == clips == inv == 1

    green = [a for a in audit_rows() if a["result"] == "成功"]
    assert len(green) == 1
    assert green[0]["证据"]["可重放"]


def test_orphan_success_ledger_row_is_rolled_back():
    """旁路写出的成功流水没有册行/段/目录键 → 读面一律回退，占用照旧为零。"""
    vid = vault_id("测试间A")
    with db() as conn:
        conn.execute(
            "INSERT INTO register_audits(part_key, vault_id, vault_name, tier, result, reason, used_before, capacity, used_after, created_at) "
            "VALUES ('P-幽灵', ?, '测试间A', '热档', '成功', '', 0, 2, 1, '2026-01-01T00:00:00')",
            (vid,),
        )
    with db() as conn:
        ev = register_evidence.partition_evidence(conn, "P-幽灵")
        assert ev["可重放"] is False and ev["缺口"] == "册面无行"
    hits = [a for a in svc.list_register_audits() if a["part_key"] == "P-幽灵"]
    assert hits[0]["result"] == "回退"

    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    ov = c.get("/api/总览").json()
    assert ov["热档"] == 0 and ov["登记缺口"] == 1
    vaults = {v["id"]: v for v in c.get("/api/片库间").json()}
    assert vaults[vid]["在册分区数"] == 0 and vaults[vid]["登记缺口"] == 1


def test_aged_out_partition_keeps_evidence_and_drops_occupancy():
    """已在册登记被保留策略改为已过期：证据仍在，占用谓词自然归零，不算假登记。"""
    vid = vault_id("测试间A")
    svc.create_partition("P-过期", "片", "2026-01-01", vid, "热档", "reel/e/",
                         clips_of("reel/e/1.bin"))
    with db() as conn:
        conn.execute("UPDATE partitions SET status='已过期' WHERE part_key='P-过期'")
        ev = register_evidence.partition_evidence(conn, "P-过期")
        # 已过期不再计入占用，但段/目录键仍在，整笔证据仍可重放
        assert ev["可重放"] is True
        assert tier_occupied_counts(conn)["热档"] == 0
    hit = next(a for a in audit_rows() if a["part_key"] == "P-过期")
    assert hit["result"] == "成功"


def test_retention_catalog_delete_is_legitimate_not_a_gap():
    """保留策略连目录销毁属合法证据拆除，不得被误判为假登记缺口。"""
    vid = vault_id("测试间A")
    svc.create_partition("P-销毁", "片", "2020-01-01", vid, "冷档", "reel/d/",
                         clips_of("reel/d/1.bin"))
    with db() as conn:
        conn.execute("INSERT INTO retention_policies(name, max_age_days, action, active) VALUES ('快销', 1, '销毁', 1)")
        pol = conn.execute("SELECT id FROM retention_policies WHERE name='快销'").fetchone()["id"]
    from app.services import retention
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('营业日','2026-01-01') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    retention.run_retention(pol, "连目录销毁")
    with db() as conn:
        ev = register_evidence.partition_evidence(conn, "P-销毁")
    assert ev["可重放"] and ev.get("已销毁")
