"""事件类型定义：调用方投递的事实与结果。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Union


@dataclass(frozen=True)
class PaymentReceived:
    """收款成功事实。amount 为整数分。同一 payment_id 重复到达时内容一致。"""

    payment_id: str
    amount: int


@dataclass(frozen=True)
class CancelRequested:
    """申请取消。requested_at 为可信 UTC 时间（tz-aware 或视为 UTC）。"""

    requested_at: datetime


@dataclass(frozen=True)
class RefundSucceeded:
    """退款指令执行成功终态，引用 command_id。"""

    command_id: str


@dataclass(frozen=True)
class RefundFailed:
    """退款指令确定失败终态（未扣款），引用 command_id，必须补发。"""

    command_id: str


Event = Union[PaymentReceived, CancelRequested, RefundSucceeded, RefundFailed]
