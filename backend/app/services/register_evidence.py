"""登记证据重放（共唯一口径）。

登记成功灯的唯一来源：由「分区册行 ∪ 素材段行 ∪ 湖对象键 ∪ 占用谓词」
在同一事务内重放出来的证据。任何一侧不成立，整笔回滚并回退流水成功行；
读路径（分区册/目录/容量页/总览/审计）不得另缓存一份成功面。

注意：
- 保留策略「连目录销毁」会把分区置为「已销毁」并拆除素材段与目录键，
  这是合法的证据拆除，不再视为登记缺口。
- 「只标过期」只改状态、保留素材段与目录键，仍要求证据齐全。
"""
from __future__ import annotations

from app.services.core import OCCUPIED_STATUS


def _clip_rows(conn, pid: int) -> list:
    return conn.execute(
        "SELECT object_key, bytes FROM clips WHERE partition_id=? ORDER BY id", (pid,)
    ).fetchall()


def inventory_size_map(conn) -> dict:
    """湖目录对象键→字节一次取齐，供批量重放复用，避免逐行全表扫描。"""
    return {
        r["object_key"]: r["bytes"]
        for r in conn.execute("SELECT object_key, bytes FROM inventory_objects").fetchall()
    }


def partition_evidence(conn, part_key: str, inv_sizes: dict | None = None) -> dict:
    """按分区键重放一笔登记的完整证据。

    inv_sizes 可由调用方一次取齐后批量传入；不传则按需取。
    """
    p = conn.execute("SELECT * FROM partitions WHERE part_key=?", (part_key,)).fetchone()
    if p is None:
        return {
            "可重放": False,
            "缺口": "册面无行",
            "册行": False,
            "素材段": 0,
            "目录键齐": False,
            "占用计入": False,
            "status": None,
        }

    pid = p["id"]
    clips = _clip_rows(conn, pid)
    clip_sizes = {c["object_key"]: c["bytes"] for c in clips}
    if inv_sizes is None:
        inv_sizes = inventory_size_map(conn)
    missing = [k for k in clip_sizes if k not in inv_sizes]
    size_bad = [k for k, b in clip_sizes.items() if k in inv_sizes and inv_sizes[k] != b]
    occupied = (
        conn.execute(
            "SELECT COUNT(*) AS c FROM partitions WHERE id=? AND vault_id=? AND status=?",
            (pid, p["vault_id"], OCCUPIED_STATUS),
        ).fetchone()["c"]
        == 1
    )

    if p["status"] == "已销毁":
        # 保留策略合法拆除：登记曾成功，证据不再强求留存。
        return {
            "可重放": True,
            "缺口": "",
            "册行": True,
            "素材段": len(clips),
            "目录键齐": not missing,
            "占用计入": occupied,
            "status": p["status"],
            "已销毁": True,
        }

    if not clips:
        gap = "素材段无行"
    elif missing:
        gap = "目录无键：" + ",".join(missing[:5]) + ("…" if len(missing) > 5 else "")
    elif size_bad:
        gap = "字节不一致：" + ",".join(size_bad[:5]) + ("…" if len(size_bad) > 5 else "")
    elif p["status"] == OCCUPIED_STATUS and not occupied:
        gap = "占用未计入"
    else:
        gap = ""

    return {
        "可重放": not gap,
        "缺口": gap,
        "册行": True,
        "素材段": len(clips),
        "目录键齐": not missing,
        "占用计入": occupied,
        "status": p["status"],
        "已销毁": False,
    }


def replay_ok(conn, part_key: str) -> bool:
    return partition_evidence(conn, part_key)["可重放"]


def reconcile_register_audits(conn) -> int:
    """把与证据对不上的成功流水回退，证据恢复则复位。

    这是「回退流水成功行」的唯一落点：成功 result 不再是写入口的一纸宣称，
    而是每次核对时由证据重放裁决。
    """
    changed = 0
    inv_sizes = inventory_size_map(conn)
    rows = conn.execute(
        "SELECT id, part_key, result FROM register_audits WHERE result IN ('成功','回退','成功注记')"
    ).fetchall()
    for r in rows:
        ev = partition_evidence(conn, r["part_key"], inv_sizes)
        if r["result"] in ("成功", "成功注记") and not ev["可重放"]:
            conn.execute(
                "UPDATE register_audits SET result='回退', reason=? WHERE id=?",
                (f"证据不成立：{ev['缺口']}", r["id"]),
            )
            changed += 1
        elif r["result"] == "回退" and ev["可重放"]:
            conn.execute(
                "UPDATE register_audits SET result='成功', reason='' WHERE id=?", (r["id"],)
            )
            changed += 1
    return changed


def register_gaps(conn) -> list[dict]:
    """当前未清的登记缺口：成功/已回退流水里证据仍不成立的那些。"""
    gaps = []
    inv_sizes = inventory_size_map(conn)
    rows = conn.execute(
        "SELECT * FROM register_audits WHERE result IN ('成功','回退','成功注记') ORDER BY id"
    ).fetchall()
    for r in rows:
        ev = partition_evidence(conn, r["part_key"], inv_sizes)
        if not ev["可重放"]:
            gaps.append(
                {
                    "audit_id": r["id"],
                    "part_key": r["part_key"],
                    "vault_id": r["vault_id"],
                    "vault_name": r["vault_name"],
                    "result": r["result"],
                    "缺口": ev["缺口"],
                }
            )
    return gaps
