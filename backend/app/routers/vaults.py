from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.core import vault_partition_count
from app.services import register_evidence

router = APIRouter(tags=["片库间"])


@router.get("/片库间")
def list_vaults():
    with db() as conn:
        gaps_by_vault = {}
        for g in register_evidence.register_gaps(conn):
            gaps_by_vault[g["vault_id"]] = gaps_by_vault.get(g["vault_id"], 0) + 1
        out = []
        for v in conn.execute("SELECT * FROM vaults ORDER BY id").fetchall():
            d = dict(v)
            # 与登记闸口同一占用谓词，容量页不得另算
            used = vault_partition_count(conn, v["id"])
            d["在册分区数"] = used
            d["剩余可登记"] = max(v["capacity"] - used, 0)
            d["是否满容"] = used >= v["capacity"]
            # 该库仍有未清登记缺口的笔数：容量页必须与总览同一口径显性暴露
            d["登记缺口"] = gaps_by_vault.get(v["id"], 0)
            out.append(d)
        return out
