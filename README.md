# 取消退款协调库

Python + SQLite 实现的取消退款协调库。收款事实与退款结果由调用方投递，
允许乱序、重复及取消后迟到；库本身不执行真实支付，只维护账务与退款指令。
金额统一为整数分。

## 用法

```python
from datetime import datetime, timezone
from refund_coordinator import (
    Coordinator, PaymentReceived, CancelRequested,
    RefundSucceeded, RefundFailed,
)

c = Coordinator("refunds.db")          # 或 ":memory:"
t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# CreateOrder：保存订单标识、UTC 创建时间与规则快照（默认 8500 基点）
c.create_order("order-1", t0)                    # 默认 r=8500
c.create_order("order-2", t0, ratio_bps=5000)    # 自定义中间档比例

# Apply：投递事件（event_id 全局去重，重复事件整体幂等）
c.apply("order-1", "evt-1", PaymentReceived("pay-1", 10100))
c.apply("order-1", "evt-2", CancelRequested(t0.replace(hour=30)))

# Read：原子读取账务快照与全部退款指令
snap = c.read("order-1")
for cmd in snap.commands:
    if cmd.status == "pending":
        ...  # 调用方执行真实退款后回报终态：
        c.apply("order-1", "evt-3", RefundSucceeded(cmd.command_id))
        # 或 c.apply("order-1", "evt-3", RefundFailed(cmd.command_id))  # 自动补发新指令
```

## 规则摘要

- 收款按 `payment_id` 去重入账；取消比例由首个成功提交的取消固定：
  距创建 <24h 为 10000 基点，<48h 为快照比例 r（默认 8500），≥48h 为 0，
  并适用于当时已记账及以后迟到的全部收款。
- 不变式（取消后任一已提交快照）：`成功退款 S + 未决退款 P == ⌊去重收款 C × 比例 / 10000⌋`。
- 每条指令标识唯一、绑定一笔 `payment_id`、金额为正且不可变、不删除；
  确定失败的指令保留原记录并补发新标识指令。
- 事件去重、账务变化与新增指令在同一 SQLite 事务中原子提交。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：重复收款/取消/回报、取消后尾款迟到、8500 基点下 101 分 + 1 分、
恰满 24h/48h 边界、失败补发与重复失败幂等，并核验账务守恒与单笔上限。
