from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.core import tier_occupied_counts, vault_partition_count
from app.services import register_evidence

router = APIRouter(tags=["总览"])


@router.get("/总览")
def overview():
    with db() as conn:
        def cnt(sql, *a):
            return conn.execute(sql, a).fetchone()["c"]

        # 热温冷在册徽章：与登记写入侧同一占用谓词聚合。
        # 不用任何旁路缓存覆盖：分区册有行才计数，否则此处为零。
        tiers = tier_occupied_counts(conn)
        vaults = []
        for v in conn.execute("SELECT id, name, capacity, active FROM vaults ORDER BY id").fetchall():
            d = dict(v)
            d["在册"] = vault_partition_count(conn, v["id"])
            d["是否满容"] = d["在册"] >= v["capacity"]
            vaults.append(d)
        gaps = register_evidence.register_gaps(conn)
        return {
            **tiers,
            "在册总数": sum(tiers.values()),
            "迁移执行中": cnt("SELECT COUNT(*) AS c FROM migrate_jobs WHERE status='执行中'"),
            "待销毁候选": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已过期'"),
            "已销毁": cnt("SELECT COUNT(*) AS c FROM partitions WHERE status='已销毁'"),
            "目录对象": cnt("SELECT COUNT(*) AS c FROM inventory_objects"),
            "登记缺口": len(gaps),
            "缺口明细": gaps,
            "最近互证": (
                lambda r: dict(r) if r else None
            )(conn.execute("SELECT status, drift_count, created_at FROM verify_runs ORDER BY id DESC LIMIT 1").fetchone()),
            "片库间": vaults,
        }
