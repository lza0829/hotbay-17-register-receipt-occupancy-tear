from __future__ import annotations
from fastapi import HTTPException
from app.db import db
from app.services.core import (
    OCCUPIED_STATUS,
    now,
    require_tier,
    vault_partition_count,
    write_register_audit,
)
from app.services import register_evidence


def list_partitions(conn=None):
    def _q(c):
        rows = c.execute(
            '''SELECT p.*, v.name AS vault_name,
                      (SELECT COUNT(*) FROM clips c WHERE c.partition_id=p.id) AS clip_count,
                      (SELECT COALESCE(SUM(bytes),0) FROM clips c WHERE c.partition_id=p.id) AS total_bytes
               FROM partitions p JOIN vaults v ON v.id=p.vault_id
               ORDER BY p.id DESC'''
        ).fetchall()
        out = []
        inv_sizes = register_evidence.inventory_size_map(c)
        for r in rows:
            d = dict(r)
            # 册页不单方面宣称在册：每行附带证据重放结果，
            # 目录缺键/段缺失的行前端必须显性标缺口，不得当成功行展示。
            ev = register_evidence.partition_evidence(c, r["part_key"], inv_sizes)
            d["证据可重放"] = ev["可重放"]
            d["证据缺口"] = ev["缺口"]
            out.append(d)
        return out

    if conn is not None:
        return _q(conn)
    with db() as c:
        return _q(c)


def list_register_audits():
    """登记流水：结果由证据重放裁决，不采信写入时的宣称。

    流水行本身保留（审计不可篡改），但「成功」只有在分区册行、素材段、
    湖对象键、占用谓词四侧同命运重放通过时才对外显示；否则显示「回退」
    并给出缺口原因。这样任何读面都不可能对着一条无证据的成功单号亮绿灯。
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM register_audits ORDER BY id DESC LIMIT 200"
        ).fetchall()
        out = []
        inv_sizes = register_evidence.inventory_size_map(conn)
        for r in rows:
            d = dict(r)
            if d["result"] in ("成功", "回退", "成功注记"):
                ev = register_evidence.partition_evidence(conn, d["part_key"], inv_sizes)
                if not ev["可重放"]:
                    d["result"] = "回退"
                    d["缺口"] = ev["缺口"]
                    d["reason"] = d["reason"] or f"证据不成立：{ev['缺口']}"
                else:
                    if d["result"] != "成功":
                        d["result"] = "成功"
                    d["缺口"] = ""
                d["证据"] = ev
            else:
                d["缺口"] = ""
            out.append(d)
        return out


def create_partition(part_key: str, title: str, release_day: str, vault_id: int, tier: str, object_prefix: str, clips: list):
    """分区登记（臂A：以占用∪册面∪目录键可重放为准）。

    - BEGIN IMMEDIATE 把闸口判定与全部写入序列化进同一临界区，
      并发双登记同一库由库级写锁排队，后到的重放同一占用谓词，不得双破硬闸。
    - 分区行、素材段行、目录键行、成功流水在同一事务提交；提交前必须由
      上面四侧重放出该笔成功的证据，重放不过即整笔抛错回滚，绝不留
      「单号绿、证据无」的半截。
    - 失败另起事务补失败流水（占用前后相等），证明回到登记前快照。
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
            # 成功灯的唯一凭据：四侧同命运重放。不过即整笔回滚。
            ev = register_evidence.partition_evidence(conn, part_key)
            if not ev["可重放"]:
                raise HTTPException(
                    500, detail=f"登记证据重放不成立（{ev['缺口']}），整笔回滚"
                )
            used_after = vault_partition_count(conn, vault_id)
            if used_after != used + 1:
                raise HTTPException(
                    500,
                    detail=f"占用谓词重放不一致（{used}→{used_after}，应为{used + 1}），整笔回滚",
                )
            audit["used_after"] = used_after
            audit_id = write_register_audit(conn, **audit, result="成功")
            # 顺带把历史成功流水与证据对账（同一临界区，不会另起写锁）
            register_evidence.reconcile_register_audits(conn)
            return {
                "结果": "成功",
                "ok": True,
                "register_ok": True,
                "audit_id": audit_id,
                "id": pid,
                "part_key": part_key,
                "tier": tier,
                "vault_id": vault_id,
                "vault_name": v["name"],
                "额定": v["capacity"],
                "占用前": used,
                "占用后": used_after,
                "证据": ev,
                "created_at": ts,
            }
    except HTTPException as e:
        # 登记事务已整体回滚到登记前快照；另起事务补失败流水，
        # 占用后=占用前，供读路径核对「失败登记不计入占用」。
        audit["used_after"] = audit["used_before"]
        with db(immediate=True) as conn:
            write_register_audit(conn, **audit, result="失败", reason=e.detail or "")
        raise
