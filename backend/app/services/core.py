from __future__ import annotations
from datetime import datetime
from fastapi import HTTPException
from app.config import TIER_ORDER, TIERS

# 占用谓词全库唯一口径：分区状态为「在册」即占用片库间额定名额。
# 写入侧（登记闸口）与全部读路径（容量页/总览徽章/审计流水）必须共用
# 下面两个函数，禁止各自另写 status 过滤条件。
OCCUPIED_STATUS = "在册"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def require_tier(tier: str) -> str:
    if tier not in TIERS:
        raise HTTPException(400, detail=f"温层只能是{'/'.join(TIERS)}")
    return tier


def vault_partition_count(conn, vault_id: int) -> int:
    """额定占用谓词：某片库间当前在册（占用额定）的分区数。"""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM partitions WHERE vault_id=? AND status=?",
        (vault_id, OCCUPIED_STATUS),
    ).fetchone()["c"]


def tier_occupied_counts(conn) -> dict:
    """总览热温冷在册徽章：与写入侧同一占用谓词，按温层聚合。"""
    rows = conn.execute(
        "SELECT tier, COUNT(*) AS c FROM partitions WHERE status=? GROUP BY tier",
        (OCCUPIED_STATUS,),
    ).fetchall()
    counts = {r["tier"]: r["c"] for r in rows}
    return {tier: counts.get(tier, 0) for tier in TIERS}


def partition_row(conn, pid: int):
    row = conn.execute("SELECT * FROM partitions WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, detail="分区不存在")
    return row


def can_migrate(from_tier: str, to_tier: str) -> bool:
    return TIER_ORDER[to_tier] != TIER_ORDER[from_tier]


def write_register_audit(conn, *, part_key, vault_id, vault_name, tier,
                         result, reason="", used_before=None, capacity=None,
                         used_after=None) -> int:
    """登记审计流水。成功时必须在登记同一事务内写入（与分区/目录同成败）；
    失败时由登记方在回滚后另起事务补写，占用前后相等，证明回到登记前快照。"""
    cur = conn.execute(
        '''INSERT INTO register_audits(part_key, vault_id, vault_name, tier, result,
                                       reason, used_before, capacity, used_after, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (part_key, vault_id, vault_name, tier, result, reason,
         used_before, capacity, used_after, now()),
    )
    return cur.lastrowid

def surface_register_hint(part_key: str) -> dict:
    return {"part_key": part_key, "panel": "register"}

