"""类型定义：事件、快照、指令、账务视图。金额一律为整数分。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class EventType(str, Enum):
    PAYMENT_SUCCEEDED = "payment_succeeded"
    CANCEL_REQUESTED = "cancel_requested"
    REFUND_SUCCEEDED = "refund_succeeded"
    REFUND_FAILED = "refund_failed"


@dataclass(frozen=True)
class Event:
    """调用方投递的事实。event_id 全局唯一标识一次投递。"""
    event_id: str
    type: EventType
    payment_id: Optional[str] = None   # PAYMENT_SUCCEEDED
    amount: Optional[int] = None       # PAYMENT_SUCCEEDED，单位：分
    requested_at: Optional[str] = None  # CANCEL_REQUESTED，可信 UTC ISO8601
    command_id: Optional[str] = None   # REFUND_SUCCEEDED / REFUND_FAILED


@dataclass(frozen=True)
class RefundCommand:
    """退款指令：标识、归属 payment、金额均不可变。"""
    command_id: str
    order_id: str
    payment_id: str
    amount: int
    status: str  # pending / succeeded / failed


@dataclass(frozen=True)
class LedgerSnapshot:
    """某订单的账务视图。"""
    order_id: str
    cancelled: bool
    ratio_bps: Optional[int]          # 取消后固定的退款比例（基点）
    collected_total: int              # C：去重后的收款总额
    refunded_total: int               # S：已成功退款总额
    pending_total: int                # P：已发布但无终态的退款总额
    target_total: int                 # floor(C * ratio / 10000)，未取消为 0
