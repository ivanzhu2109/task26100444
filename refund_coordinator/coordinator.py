"""取消退款协调器：CreateOrder / Apply / Read。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .events import (
    CancelRequested,
    Event,
    PaymentReceived,
    RefundFailed,
    RefundSucceeded,
)
from .store import Store

DEFAULT_RATIO_BPS = 8500
DAY_SECONDS = 24 * 3600


@dataclass(frozen=True)
class RefundCommand:
    """退款指令：标识、归属、金额不可变。"""

    command_id: str
    payment_id: str
    amount: int
    status: str  # pending | succeeded | failed


@dataclass(frozen=True)
class Snapshot:
    """账务快照：C 去重收款总额，S 成功退款，P 未决退款，T 目标退款额。"""

    order_id: str
    ratio_bps: Optional[int]  # 已固定的取消比例；未取消为 None
    collected: int
    refunded: int
    pending: int
    target: int
    commands: List[RefundCommand] = field(default_factory=list)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _to_utc(value).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class Coordinator:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    # ---- CreateOrder ----

    def create_order(
        self,
        order_id: str,
        created_at: datetime,
        ratio_bps: int = DEFAULT_RATIO_BPS,
    ) -> None:
        if not 0 <= ratio_bps <= 10000:
            raise ValueError("ratio_bps 必须在 [0, 10000] 基点内")
        with self._store.transaction() as conn:
            self._store.insert_order(conn, order_id, _iso(created_at), ratio_bps)

    # ---- Apply ----

    def apply(self, order_id: str, event_id: str, event: Event) -> None:
        with self._store.transaction() as conn:
            order = self._store.get_order(conn, order_id)
            if order is None:
                raise KeyError(f"未知订单: {order_id}")
            if not self._store.record_event(conn, order_id, event_id):
                return  # 重复 event_id：整体幂等

            if isinstance(event, PaymentReceived):
                self._apply_payment(conn, order, event)
            elif isinstance(event, CancelRequested):
                self._apply_cancel(conn, order, event)
            elif isinstance(event, RefundSucceeded):
                self._apply_result(conn, order_id, event.command_id, "succeeded")
            elif isinstance(event, RefundFailed):
                self._apply_result(conn, order_id, event.command_id, "failed")
            else:
                raise TypeError(f"未知事件类型: {type(event)!r}")

    def _apply_payment(self, conn, order, event: PaymentReceived) -> None:
        if not isinstance(event.amount, int) or event.amount <= 0:
            raise ValueError("收款金额必须为正整数分")
        order_id = order[0]
        if self._store.insert_payment(conn, order_id, event.payment_id, event.amount):
            # 仅首次入账触发再平衡；重复 payment_id 不产生任何变化
            self._rebalance(conn, order_id)

    def _apply_cancel(self, conn, order, event: CancelRequested) -> None:
        order_id, created_at, ratio_bps, cancel_ratio, _ = order
        if cancel_ratio is not None:
            return  # 首个取消已固定比例，后续取消不得改写
        elapsed = (_to_utc(event.requested_at) - _parse(created_at)).total_seconds()
        if elapsed < DAY_SECONDS:
            decided = 10000
        elif elapsed < 2 * DAY_SECONDS:
            decided = ratio_bps
        else:
            decided = 0
        self._store.set_cancel(conn, order_id, decided, _iso(event.requested_at))
        self._rebalance(conn, order_id)

    def _apply_result(self, conn, order_id: str, command_id: str, status: str) -> None:
        current = self._store.get_command_status(conn, order_id, command_id)
        if current is None:
            raise ValueError(f"未知退款指令: {command_id}")
        if current != "pending":
            return  # 终态已确定：重复回报幂等忽略
        self._store.set_command_status(conn, command_id, status)
        if status == "failed":
            # 确定失败未扣款：未决减少，必须补发新指令
            self._rebalance(conn, order_id)

    # ---- 再平衡：使 S + P == floor(C * ratio / 10000) ----

    def _rebalance(self, conn, order_id: str) -> None:
        order = self._store.get_order(conn, order_id)
        ratio = order[3]
        if ratio is None:
            return  # 未取消订单不发退款
        payments = self._store.list_payments(conn, order_id)
        commands = self._store.list_commands(conn, order_id)

        collected = sum(amount for _, amount in payments)
        target = collected * ratio // 10000
        outstanding = sum(a for _, _, a, s in commands if s in ("pending", "succeeded"))
        deficit = target - outstanding
        if deficit <= 0:
            return

        # 每笔收款的成功+未决不得超过实收：按入账顺序在剩余额度内分摊
        used = {}
        for _, pid, amount, status in commands:
            if status in ("pending", "succeeded"):
                used[pid] = used.get(pid, 0) + amount
        seq = self._store.next_command_seq(conn, order_id)
        for payment_id, amount in payments:
            if deficit == 0:
                break
            capacity = amount - used.get(payment_id, 0)
            issue = min(capacity, deficit)
            if issue <= 0:
                continue
            command_id = f"{order_id}-cmd-{seq}"
            self._store.insert_command(
                conn, command_id, order_id, payment_id, issue, seq
            )
            seq += 1
            deficit -= issue

    # ---- Read ----

    def read(self, order_id: str) -> Snapshot:
        with self._store.transaction() as conn:
            order = self._store.get_order(conn, order_id)
            if order is None:
                raise KeyError(f"未知订单: {order_id}")
            ratio = order[3]
            payments = self._store.list_payments(conn, order_id)
            rows = self._store.list_commands(conn, order_id)

        collected = sum(amount for _, amount in payments)
        refunded = sum(a for _, _, a, s in rows if s == "succeeded")
        pending = sum(a for _, _, a, s in rows if s == "pending")
        target = collected * ratio // 10000 if ratio is not None else 0
        commands = [
            RefundCommand(command_id=cid, payment_id=pid, amount=a, status=s)
            for cid, pid, a, s in rows
        ]
        return Snapshot(
            order_id=order_id,
            ratio_bps=ratio,
            collected=collected,
            refunded=refunded,
            pending=pending,
            target=target,
            commands=commands,
        )
