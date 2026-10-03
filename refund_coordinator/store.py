"""SQLite 持久化层：schema 与原子读写。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,          -- UTC ISO8601
    ratio_bps    INTEGER NOT NULL,       -- 中间档退款比例快照
    cancelled    INTEGER NOT NULL DEFAULT 0,
    cancel_ratio_bps INTEGER             -- 首个取消固定的比例
);
CREATE TABLE IF NOT EXISTS payments (
    order_id   TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    PRIMARY KEY (order_id, payment_id)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    type     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'  -- pending/succeeded/failed
);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
