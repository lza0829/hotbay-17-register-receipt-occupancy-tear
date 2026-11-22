"""登记占用闸（臂A：以额定占用闸为准）联动一致性测试。

覆盖：满容再登记、并发抢最后名额、半截失败脏写、回执/容量页/总览/审计同谓词重放。
"""
import concurrent.futures as cf

import pytest
from fastapi import HTTPException

from app.db import db
from app.services import partitions as svc
from app.services.core import tier_occupied_counts, vault_partition_count
from conftest import vault_id


def clips_of(*keys):
    return [{"object_key": k, "bytes": 100} for k in keys]


def counts():
    with db() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("partitions", "clips", "inventory_objects", "register_audits")
        }


def test_full_vault_reregistration_fails_clean():
    """满容再登记：整笔拒绝，分区/素材段/目录零增量，审计记失败且占用不变。"""
    vid = vault_id("测试间B")  # 额定 1
    r = svc.create_partition("P-满-1", "片一", "2026-01-01", vid, "热档", "reel/t/", clips_of("reel/t/a.bin"))
    assert (r["占用前"], r["占用后"], r["额定"]) == (0, 1, 1)

    with pytest.raises(HTTPException) as ei:
        svc.create_partition("P-满-2", "片二", "2026-01-01", vid, "热档", "reel/t/", clips_of("reel/t/b.bin"))
    assert ei.value.status_code == 400
    assert "已满" in ei.value.detail

    assert counts() == {"partitions": 1, "clips": 1, "inventory_objects": 1, "register_audits": 2}
    with db() as conn:
        assert vault_partition_count(conn, vid) == 1
        fail = conn.execute("SELECT * FROM register_audits WHERE result='失败'").fetchone()
        assert fail["part_key"] == "P-满-2"
        assert (fail["used_before"], fail["used_after"], fail["capacity"]) == (1, 1, 1)


def test_concurrent_last_slot_no_double_break():
    """并发抢最后名额：8 线程抢 1 个剩余名额，恰 1 笔成功，不得双破硬闸。"""
    vid = vault_id("测试间A")  # 额定 2，先占 1，仅剩 1 名额
    svc.create_partition("P-先占", "片零", "2026-01-01", vid, "热档", "reel/c/", clips_of("reel/c/0.bin"))

    n = 8

    def attempt(i):
        try:
            svc.create_partition(f"P-抢-{i}", "片", "2026-01-01", vid, "温档", "reel/c/",
                                 clips_of(f"reel/c/{i}.bin"))
            return "成功"
        except HTTPException:
            return "失败"

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(attempt, range(1, n + 1)))

    assert results.count("成功") == 1, results
    assert results.count("失败") == n - 1, results
    with db() as conn:
        assert vault_partition_count(conn, vid) == 2  # 到额定为止
        got = {r["result"]: r["c"] for r in conn.execute(
            "SELECT result, COUNT(*) AS c FROM register_audits GROUP BY result")}
        assert got == {"成功": 2, "失败": n - 1}  # 先占 1 + 抢到 1；其余全失败留痕
    assert counts()["partitions"] == 2
    assert counts()["inventory_objects"] == 2  # 目录只含成功两笔的对象


def test_mid_failure_rolls_back_to_snapshot():
    """半截失败脏写：第二段撞上已登记对象键，整笔回到登记前快照。"""
    vid = vault_id("测试间A")
    svc.create_partition("P-旧", "旧片", "2026-01-01", vid, "冷档", "reel/x/", clips_of("reel/x/dup.bin"))
    before = counts()

    with pytest.raises(HTTPException) as ei:
        svc.create_partition("P-半截", "新片", "2026-01-01", vid, "冷档", "reel/y/",
                             clips_of("reel/y/new.bin", "reel/x/dup.bin"))
    assert "对象键重复" in ei.value.detail

    after = counts()
    assert after["partitions"] == before["partitions"]
    assert after["clips"] == before["clips"]
    assert after["inventory_objects"] == before["inventory_objects"]
    assert after["register_audits"] == before["register_audits"] + 1  # 只多一条失败审计
    with db() as conn:
        assert vault_partition_count(conn, vid) == 1  # 占用回到登记前
        fail = conn.execute("SELECT * FROM register_audits ORDER BY id DESC").fetchone()
        assert fail["result"] == "失败" and fail["used_before"] == fail["used_after"] == 1


def test_batch_duplicate_keys_fail():
    """同笔内对象键重复：整笔拒绝，不留任何增量。"""
    vid = vault_id("测试间A")
    with pytest.raises(HTTPException) as ei:
        svc.create_partition("P-重键", "片", "2026-01-01", vid, "热档", "reel/z/",
                             clips_of("reel/z/a.bin", "reel/z/a.bin"))
    assert "同笔素材对象键重复" in ei.value.detail
    assert counts()["partitions"] == 0


def test_existing_catalog_key_not_silently_overwritten():
    """目录键行已存在（如只删元数据销毁残留）：整笔失败，绝不 INSERT OR REPLACE 静默抹改。"""
    vid = vault_id("测试间A")
    with db() as conn:
        conn.execute(
            "INSERT INTO inventory_objects(object_key, bytes, vault_name, scanned_at) VALUES ('reel/k/legacy.bin', 5, '测试间A', '2026-01-01T00:00:00')")
    with pytest.raises(HTTPException) as ei:
        svc.create_partition("P-撞目录", "片", "2026-01-01", vid, "热档", "reel/k/",
                             clips_of("reel/k/legacy.bin"))
    assert "目录对象键已存在" in ei.value.detail
    with db() as conn:
        row = conn.execute("SELECT * FROM inventory_objects WHERE object_key='reel/k/legacy.bin'").fetchone()
        assert row["bytes"] == 5  # 原目录键行原样保留，未被改写
        assert conn.execute("SELECT COUNT(*) AS c FROM partitions").fetchone()["c"] == 0


def test_receipt_and_all_read_paths_replay_same_predicate():
    """回执、容量页、总览徽章、审计流水同刷：同一占用谓词重放结果一致。"""
    vid = vault_id("测试间A")
    r = svc.create_partition("P-一致", "片", "2026-01-01", vid, "温档", "reel/m/",
                             clips_of("reel/m/1.bin", "reel/m/2.bin"))
    with db() as conn:
        used = vault_partition_count(conn, vid)
        tiers = tier_occupied_counts(conn)
        audit = conn.execute("SELECT * FROM register_audits WHERE id=?", (r["audit_id"],)).fetchone()
    assert r["占用后"] == used == audit["used_after"] == 1
    assert r["占用前"] == audit["used_before"] == 0
    assert r["额定"] == audit["capacity"] == 2
    assert tiers["温档"] == 1 and tiers["热档"] == 0 and tiers["冷档"] == 0
    assert audit["result"] == "成功" and audit["vault_name"] == "测试间A"


def test_http_surfaces_agree():
    """HTTP 面：登记回执 / 片库间容量页 / 总览 / 登记审计 同笔一致。"""
    from fastapi.testclient import TestClient
    from app.main import app

    c = TestClient(app)
    vid = vault_id("测试间A")
    resp = c.post("/api/分区", json={
        "part_key": "P-HTTP", "title": "片", "release_day": "2026-01-01",
        "vault_id": vid, "tier": "热档", "object_prefix": "reel/h/",
        "clips": [{"object_key": "reel/h/1.bin", "bytes": 7}],
    })
    assert resp.status_code == 200, resp.text
    receipt = resp.json()

    vaults = {v["id"]: v for v in c.get("/api/片库间").json()}
    assert vaults[vid]["在册分区数"] == receipt["占用后"] == 1
    assert vaults[vid]["剩余可登记"] == 1
    assert vaults[vid]["是否满容"] is False

    ov = c.get("/api/总览").json()
    assert ov["热档"] == 1
    assert {v["id"]: v for v in ov["片库间"]}[vid]["在册"] == 1

    audits = c.get("/api/登记审计").json()
    assert audits[0]["id"] == receipt["audit_id"]
    assert audits[0]["result"] == "成功" and audits[0]["used_after"] == 1

    # 目录清单与分区册同笔落账
    inv = c.get("/api/目录清单").json()
    assert [x["object_key"] for x in inv] == ["reel/h/1.bin"]
    parts = c.get("/api/分区").json()
    assert parts[0]["part_key"] == "P-HTTP" and parts[0]["clip_count"] == 1
