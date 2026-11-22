from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS vaults (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  capacity INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  release_day TEXT NOT NULL,
  vault_id INTEGER NOT NULL,
  tier TEXT NOT NULL,
  object_prefix TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '在册',
  created_at TEXT NOT NULL,
  FOREIGN KEY(vault_id) REFERENCES vaults(id)
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  entered_day TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS migrate_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  from_tier TEXT NOT NULL,
  to_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id)
);
CREATE TABLE IF NOT EXISTS retention_policies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  max_age_days INTEGER NOT NULL,
  action TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS destroy_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_id INTEGER NOT NULL,
  policy_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(partition_id) REFERENCES partitions(id),
  FOREIGN KEY(policy_id) REFERENCES retention_policies(id)
);
CREATE TABLE IF NOT EXISTS inventory_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  object_key TEXT NOT NULL UNIQUE,
  bytes INTEGER NOT NULL,
  vault_name TEXT NOT NULL,
  scanned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL,
  drift_count INTEGER NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS register_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  part_key TEXT NOT NULL,
  vault_id INTEGER,
  vault_name TEXT,
  tier TEXT NOT NULL,
  result TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  used_before INTEGER,
  capacity INTEGER,
  used_after INTEGER,
  created_at TEXT NOT NULL
);
'''


def connect() -> sqlite3.Connection:
    # isolation_level=None：事务边界全由 db() 显式 BEGIN/COMMIT 控制，
    # 这样登记写路径才能用 BEGIN IMMEDIATE 在闸口判定前拿到写锁。
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发登记抢同一写锁时等待而不是立刻 database is locked
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def db(immediate: bool = False):
    """单事务上下文：提交或整体回滚，不存在半截写入。

    immediate=True 时以 BEGIN IMMEDIATE 开启，进入即取库级写锁，
    把「占用闸判定 + 分区/素材段/目录写入」序列化进同一临界区。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # WAL：读路径（容量页/总览/目录清单）不被登记写事务阻塞
        conn.execute("PRAGMA journal_mode=WAL")
