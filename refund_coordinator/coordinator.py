"""取消退款协调器：CreateOrder / Apply / Read。

不变量（取消后任一已提交快照）：
    S + P == floor(C * ratio / 10000)
其中 C 为去重收款总额，S 为成功退款，P 为已发布无终态退款。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from .models import Event, EventType, LedgerSnapshot, RefundCommand
from .store import Store

DEFAULT_RATIO_BPS = 8500
_FULL_BPS = 10000
_DAY = timedelta(hours=24)


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Coordinator:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.store = Store(db_path)

    # ---- CreateOrder ----
    def create_order(self, order_id: str, created_at: str,
                     ratio_bps: int = DEFAULT_RATIO_BPS) -> None:
        created = _parse_utc(created_at)  # 校验时间格式
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO orders(order_id, created_at, ratio_bps) VALUES (?,?,?)",
                (order_id, created.isoformat(), ratio_bps),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ---- Apply ----
    def apply(self, order_id: str, event: Event) -> None:
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 事件级去重：同一 event_id 重复投递直接幂等返回
            dup = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if dup:
                conn.execute("COMMIT")
                return

            etype = EventType(event.type)
            if etype is EventType.PAYMENT_SUCCEEDED:
                self._apply_payment(conn, order_id, event)
            elif etype is EventType.CANCEL_REQUESTED:
                self._apply_cancel(conn, order_id, event)
            elif etype is EventType.REFUND_SUCCEEDED:
                self._apply_refund_result(conn, order_id, event, success=True)
            elif etype is EventType.REFUND_FAILED:
                self._apply_refund_result(conn, order_id, event, success=False)

            conn.execute(
                "INSERT INTO events(event_id, order_id, type) VALUES (?,?,?)",
                (event.event_id, order_id, etype.value),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _apply_payment(self, conn, order_id: str, event: Event) -> None:
        if event.payment_id is None or event.amount is None or event.amount <= 0:
            raise ValueError("payment event requires payment_id and positive amount")
        # 同一 payment_id 只入账一次（重复事实内容一致）
        cur = conn.execute(
            "INSERT OR IGNORE INTO payments(order_id, payment_id, amount) VALUES (?,?,?)",
            (order_id, event.payment_id, event.amount),
        )
        if cur.rowcount == 0:
            return  # 重复收款，已入账过
        if self._is_cancelled(conn, order_id):
            self._rebalance(conn, order_id)

    def _apply_cancel(self, conn, order_id: str, event: Event) -> None:
        if event.requested_at is None:
            raise ValueError("cancel event requires requested_at")
        if self._is_cancelled(conn, order_id):
            return  # 后续取消不得改写决定
        row = conn.execute(
            "SELECT created_at, ratio_bps FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown order: {order_id}")
        created_at, ratio_bps = _parse_utc(row[0]), row[1]
        elapsed = _parse_utc(event.requested_at) - created_at
        if elapsed < _DAY:
            ratio = _FULL_BPS
        elif elapsed < 2 * _DAY:
            ratio = ratio_bps
        else:
            ratio = 0
        conn.execute(
            "UPDATE orders SET cancelled = 1, cancel_ratio_bps = ? WHERE order_id = ?",
            (ratio, order_id),
        )
        self._rebalance(conn, order_id)

    def _apply_refund_result(self, conn, order_id: str, event: Event,
                             success: bool) -> None:
        if event.command_id is None:
            raise ValueError("refund result requires command_id")
        row = conn.execute(
            "SELECT status FROM commands WHERE command_id = ? AND order_id = ?",
            (event.command_id, order_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown command: {event.command_id}")
        if row[0] != "pending":
            return  # 终态已确定，重复回报幂等忽略
        conn.execute(
            "UPDATE commands SET status = ? WHERE command_id = ?",
            ("succeeded" if success else "failed", event.command_id),
        )
        if not success:
            # 确定失败未扣款：补发新指令（旧指令保留，新指令用新标识）
            self._rebalance(conn, order_id)

    # ---- 再平衡：使 S + P == floor(C * ratio / 10000) ----
    def _rebalance(self, conn, order_id: str) -> None:
        row = conn.execute(
            "SELECT cancel_ratio_bps FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        ratio = row[0]
        collected = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        target = collected * ratio // _FULL_BPS
        committed = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM commands "
            "WHERE order_id = ? AND status IN ('pending','succeeded')",
            (order_id,),
        ).fetchone()[0]
        deficit = target - committed
        if deficit <= 0:
            return
        # 按 payment 维度补发，单笔不超过其实收减去已承诺（成功+未决）部分
        payments = conn.execute(
            "SELECT payment_id, amount FROM payments WHERE order_id = ? ORDER BY payment_id",
            (order_id,),
        ).fetchall()
        for payment_id, amount in payments:
            if deficit <= 0:
                break
            used = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM commands "
                "WHERE order_id = ? AND payment_id = ? AND status IN ('pending','succeeded')",
                (order_id, payment_id),
            ).fetchone()[0]
            room = amount - used
            if room <= 0:
                continue
            issue = min(room, deficit)
            conn.execute(
                "INSERT INTO commands(command_id, order_id, payment_id, amount, status) "
                "VALUES (?,?,?,?, 'pending')",
                (uuid.uuid4().hex, order_id, payment_id, issue),
            )
            deficit -= issue

    def _is_cancelled(self, conn, order_id: str) -> bool:
        row = conn.execute(
            "SELECT cancelled FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown order: {order_id}")
        return bool(row[0])

    # ---- Read：原子读取账务快照 + 全部指令 ----
    def read(self, order_id: str) -> Tuple[LedgerSnapshot, List[RefundCommand]]:
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT cancelled, cancel_ratio_bps FROM orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(f"unknown order: {order_id}")
            cancelled, ratio = bool(row[0]), row[1]
            collected = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE order_id = ?",
                (order_id,),
            ).fetchone()[0]
            succeeded = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM commands "
                "WHERE order_id = ? AND status = 'succeeded'",
                (order_id,),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM commands "
                "WHERE order_id = ? AND status = 'pending'",
                (order_id,),
            ).fetchone()[0]
            commands = [
                RefundCommand(command_id=r[0], order_id=r[1], payment_id=r[2],
                              amount=r[3], status=r[4])
                for r in conn.execute(
                    "SELECT command_id, order_id, payment_id, amount, status "
                    "FROM commands WHERE order_id = ? ORDER BY rowid",
                    (order_id,),
                ).fetchall()
            ]
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        target = collected * ratio // _FULL_BPS if cancelled else 0
        snapshot = LedgerSnapshot(
            order_id=order_id,
            cancelled=cancelled,
            ratio_bps=ratio if cancelled else None,
            collected_total=collected,
            refunded_total=succeeded,
            pending_total=pending,
            target_total=target,
        )
        return snapshot, commands
