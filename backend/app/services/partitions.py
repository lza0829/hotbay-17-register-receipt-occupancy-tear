from __future__ import annotations
from fastapi import HTTPException
from app.db import db
from app.services.core import (
    now,
    require_tier,
    vault_partition_count,
    write_register_audit,
)
from app.services import register_surface


def list_partitions(conn=None):
    def _q(c):
        rows = c.execute(
            '''SELECT p.*, v.name AS vault_name,
                      (SELECT COUNT(*) FROM clips c WHERE c.partition_id=p.id) AS clip_count,
                      (SELECT COALESCE(SUM(bytes),0) FROM clips c WHERE c.partition_id=p.id) AS total_bytes
               FROM partitions p JOIN vaults v ON v.id=p.vault_id
               ORDER BY p.id DESC'''
        ).fetchall()
        return [dict(r) for r in rows]

    if conn is not None:
        return _q(conn)
    with db() as c:
        return _q(c)


def list_register_audits():
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM register_audits ORDER BY id DESC LIMIT 200"
            ).fetchall()
        ]


def create_partition(part_key: str, title: str, release_day: str, vault_id: int, tier: str, object_prefix: str, clips: list):
    """分区登记（臂A：以额定占用闸为准）。

    - BEGIN IMMEDIATE 把闸口判定与全部写入序列化进同一临界区，
      并发双登记同一库由库级写锁排队，后到的重放同一占用谓词，不得双破硬闸。
    - 分区行、素材段行、目录键行、成功审计在同一事务提交；任一失败整笔
      回滚到登记前快照，再另起事务补失败审计（占用前后相等）。
    """
    audit = {
        "part_key": part_key,
        "vault_id": vault_id,
        "vault_name": None,
        "tier": tier,
        "used_before": None,
        "capacity": None,
        "used_after": None,
    }
    try:
        tier = require_tier(tier)
        audit["tier"] = tier
        with db(immediate=True) as conn:
            v = conn.execute("SELECT * FROM vaults WHERE id=?", (vault_id,)).fetchone()
            if not v or not v["active"]:
                raise HTTPException(400, detail="片库间不可用")
            audit["vault_name"] = v["name"]
            audit["capacity"] = v["capacity"]
            # 占用闸：与所有读路径同一谓词
            used = vault_partition_count(conn, vault_id)
            audit["used_before"] = used
            if used >= v["capacity"]:
                raise HTTPException(400, detail=f"片库间分区已满（在册{used}/额定{v['capacity']}），整笔拒绝登记")
            if conn.execute("SELECT 1 FROM partitions WHERE part_key=?", (part_key,)).fetchone():
                raise HTTPException(400, detail="分区键已存在")
            if not clips:
                raise HTTPException(400, detail="至少挂一段素材元数据")
            keys = [c["object_key"] for c in clips]
            if len(set(keys)) != len(keys):
                raise HTTPException(400, detail="同笔素材对象键重复")
            for ok in keys:
                if conn.execute("SELECT 1 FROM clips WHERE object_key=?", (ok,)).fetchone():
                    raise HTTPException(400, detail=f"对象键重复：{ok}")
                if conn.execute("SELECT 1 FROM inventory_objects WHERE object_key=?", (ok,)).fetchone():
                    raise HTTPException(400, detail=f"目录对象键已存在：{ok}")
            ts = now()
            cur = conn.execute(
                "INSERT INTO partitions(part_key, title, release_day, vault_id, tier, object_prefix, status, created_at) VALUES (?,?,?,?,?,?, '在册', ?)",
                (part_key, title, release_day, vault_id, tier, object_prefix, ts),
            )
            pid = cur.lastrowid
            for clip in clips:
                ok = clip["object_key"]
                conn.execute(
                    "INSERT INTO clips(partition_id, object_key, bytes, entered_day, note) VALUES (?,?,?,?,?)",
                    (pid, ok, int(clip["bytes"]), clip.get("entered_day") or release_day, clip.get("note") or ""),
                )
                # 目录键行与分区/素材段同生同死：已存在即整笔失败，绝不静默覆盖
                conn.execute(
                    "INSERT INTO inventory_objects(object_key, bytes, vault_name, scanned_at) VALUES (?,?,?,?)",
                    (ok, int(clip["bytes"]), v["name"], ts),
                )
            # 回执占用：写入侧同一谓词在同一事务内重放
            used_after = vault_partition_count(conn, vault_id)
            audit["used_after"] = used_after
            audit_id = write_register_audit(conn, **audit, result="成功")
            return {
                "结果": "成功",
                "audit_id": audit_id,
                "id": pid,
                "part_key": part_key,
                "tier": tier,
                "vault_id": vault_id,
                "vault_name": v["name"],
                "额定": v["capacity"],
                "占用前": used,
                "占用后": used_after,
                "created_at": ts,
            }
    except HTTPException as e:
        # 登记事务已整体回滚到登记前快照；另起事务补失败审计，
        # 占用后=占用前，供读路径核对「失败登记不计入占用」。
        audit["used_after"] = audit["used_before"]
        with db() as conn:
            write_register_audit(conn, **audit, result="失败", reason=e.detail or "")
        raise

_create_partition_impl = create_partition


def create_partition(part_key: str, title: str, release_day: str, vault_id: int, tier: str, object_prefix: str, clips: list):
    out = _create_partition_impl(part_key, title, release_day, vault_id, tier, object_prefix, clips)
    return register_surface.after_create(out)

