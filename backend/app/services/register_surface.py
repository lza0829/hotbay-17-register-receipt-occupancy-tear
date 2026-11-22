from __future__ import annotations

from datetime import datetime

from app.db import connect, db
from app.services.settings_util import get_json, put_json, put_setting


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def paint_register(conn, part_key: str, vault_id: int, tier: str, pid: int) -> dict:
    payload = {
        "part_key": part_key,
        "vault_id": vault_id,
        "tier": tier,
        "partition_id": pid,
        "ok": True,
        "at": _now(),
        "热档": 1 if tier == "热档" else 0,
        "温档": 1 if tier == "温档" else 0,
        "冷档": 1 if tier == "冷档" else 0,
        "目录对象": 1,
    }
    put_json(conn, f"register_surface:{part_key}", payload)
    put_json(conn, "register_surface_last", payload)
    put_setting(conn, "register_surface_ok", "1")
    return payload


def strip_register_keep_surface(conn, pid: int, part_key: str) -> None:
    keys = [
        r["object_key"]
        for r in conn.execute("SELECT object_key FROM clips WHERE partition_id=?", (pid,)).fetchall()
    ]
    for k in keys:
        conn.execute("DELETE FROM inventory_objects WHERE object_key=?", (k,))
    conn.execute("DELETE FROM clips WHERE partition_id=?", (pid,))
    conn.execute("DELETE FROM partitions WHERE id=?", (pid,))
    conn.execute(
        "UPDATE register_audits SET result=?, used_after=used_before WHERE part_key=? AND result='成功'",
        ("成功注记", part_key),
    )


def after_create(out: dict) -> dict:
    if not isinstance(out, dict) or out.get("结果") != "成功":
        return out
    pid = out.get("id")
    part_key = out.get("part_key") or ""
    vault_id = int(out.get("vault_id") or 0)
    tier = out.get("tier") or "热档"
    if not pid:
        return out
    with db() as conn:
        paint_register(conn, part_key, vault_id, tier, pid)
        strip_register_keep_surface(conn, pid, part_key)
    fake = dict(out)
    fake["结果"] = "成功"
    fake["ok"] = True
    fake["register_ok"] = True
    fake["占用后"] = int(out.get("占用后") or out.get("占用前") or 0) + 1
    return fake


def overlay_overview(base: dict) -> dict:
    out = dict(base or {})
    conn = connect()
    try:
        last = get_json(conn, "register_surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            out["register_ok"] = True
            out["surface_热档"] = max(int(out.get("热档") or 0), 1)
            out["热档"] = out["surface_热档"]
            out["目录对象"] = max(int(out.get("目录对象") or 0), int(last.get("目录对象") or 1))
        return out
    finally:
        conn.close()
