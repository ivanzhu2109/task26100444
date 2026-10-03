"""SQLite 存储层：schema 与原子读写。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ratio_bps INTEGER NOT NULL,
    cancel_ratio_bps INTEGER,
    cancel_requested_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    order_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (order_id, event_id)
);
CREATE TABLE IF NOT EXISTS payments (
    order_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (order_id, payment_id)
);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    payment_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    seq INTEGER NOT NULL
);
"""


class Store:
    """SQLite 持久化，单连接串行事务。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path)
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- orders ----

    def insert_order(
        self, conn: sqlite3.Connection, order_id: str, created_at: str, ratio_bps: int
    ) -> None:
        conn.execute(
            "INSERT INTO orders(order_id, created_at, ratio_bps) VALUES (?,?,?)",
            (order_id, created_at, ratio_bps),
        )

    def get_order(
        self, conn: sqlite3.Connection, order_id: str
    ) -> Optional[Tuple[str, str, int, Optional[int], Optional[str]]]:
        return conn.execute(
            "SELECT order_id, created_at, ratio_bps, cancel_ratio_bps, cancel_requested_at"
            " FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()

    def set_cancel(
        self, conn: sqlite3.Connection, order_id: str, ratio_bps: int, requested_at: str
    ) -> None:
        conn.execute(
            "UPDATE orders SET cancel_ratio_bps = ?, cancel_requested_at = ?"
            " WHERE order_id = ? AND cancel_ratio_bps IS NULL",
            (ratio_bps, requested_at, order_id),
        )

    # ---- events ----

    def record_event(self, conn: sqlite3.Connection, order_id: str, event_id: str) -> bool:
        """登记 event_id；已存在返回 False（重复事件）。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO events(order_id, event_id) VALUES (?,?)",
            (order_id, event_id),
        )
        return cur.rowcount == 1

    # ---- payments ----

    def insert_payment(
        self, conn: sqlite3.Connection, order_id: str, payment_id: str, amount: int
    ) -> bool:
        """按 payment_id 去重入账；已入账返回 False。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO payments(order_id, payment_id, amount) VALUES (?,?,?)",
            (order_id, payment_id, amount),
        )
        return cur.rowcount == 1

    def list_payments(
        self, conn: sqlite3.Connection, order_id: str
    ) -> List[Tuple[str, int]]:
        return conn.execute(
            "SELECT payment_id, amount FROM payments WHERE order_id = ? ORDER BY rowid",
            (order_id,),
        ).fetchall()

    # ---- commands ----

    def next_command_seq(self, conn: sqlite3.Connection, order_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM commands WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return int(row[0])

    def insert_command(
        self,
        conn: sqlite3.Connection,
        command_id: str,
        order_id: str,
        payment_id: str,
        amount: int,
        seq: int,
    ) -> None:
        conn.execute(
            "INSERT INTO commands(command_id, order_id, payment_id, amount, status, seq)"
            " VALUES (?,?,?,?,'pending',?)",
            (command_id, order_id, payment_id, amount, seq),
        )

    def list_commands(
        self, conn: sqlite3.Connection, order_id: str
    ) -> List[Tuple[str, str, int, str]]:
        """返回 (command_id, payment_id, amount, status)，按签发顺序。"""
        return conn.execute(
            "SELECT command_id, payment_id, amount, status FROM commands"
            " WHERE order_id = ? ORDER BY seq",
            (order_id,),
        ).fetchall()

    def get_command_status(
        self, conn: sqlite3.Connection, order_id: str, command_id: str
    ) -> Optional[str]:
        row = conn.execute(
            "SELECT status FROM commands WHERE order_id = ? AND command_id = ?",
            (order_id, command_id),
        ).fetchone()
        return row[0] if row else None

    def set_command_status(
        self, conn: sqlite3.Connection, command_id: str, status: str
    ) -> None:
        conn.execute(
            "UPDATE commands SET status = ? WHERE command_id = ? AND status = 'pending'",
            (status, command_id),
        )
