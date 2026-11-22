from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services.core import vault_partition_count

router = APIRouter(tags=["片库间"])


@router.get("/片库间")
def list_vaults():
    with db() as conn:
        out = []
        for v in conn.execute("SELECT * FROM vaults ORDER BY id").fetchall():
            d = dict(v)
            # 与登记闸口同一占用谓词，容量页不得另算
            used = vault_partition_count(conn, v["id"])
            d["在册分区数"] = used
            d["剩余可登记"] = max(v["capacity"] - used, 0)
            d["是否满容"] = used >= v["capacity"]
            out.append(d)
        return out
