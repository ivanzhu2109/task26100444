# task26100444

取消退款协调库（Python + SQLite）。收款事实与退款结果由调用方投递，允许乱序、重复、取消后迟到；不执行真实支付。金额一律为整数分。

## 用法

```python
from refund_coordinator import Coordinator, Event, EventType

c = Coordinator("refunds.db")                      # 或 ":memory:"
c.create_order("order-1", "2026-01-01T00:00:00+00:00")  # 默认中间档 8500 基点

# 投递事实（可乱序/重复，event_id 全局唯一）
c.apply("order-1", Event(event_id="e1", type=EventType.PAYMENT_SUCCEEDED,
                         payment_id="pay-1", amount=101))
c.apply("order-1", Event(event_id="e2", type=EventType.CANCEL_REQUESTED,
                         requested_at="2026-01-01T12:00:00+00:00"))

# 原子读取账务快照 + 全部退款指令
snapshot, commands = c.read("order-1")
for cmd in commands:
    if cmd.status == "pending":
        ...  # 调用方执行真实退款后回报结果
        c.apply("order-1", Event(event_id="e3", type=EventType.REFUND_SUCCEEDED,
                                 command_id=cmd.command_id))
        # 或 EventType.REFUND_FAILED（确定未扣款，库内自动补发新指令）
```

## 规则摘要

- 取消比例：距创建 <24h 为 10000 基点，<48h 为快照 r（默认 8500），否则 0；首个取消固定，后续不改写。
- 不变量：取消后任一快照中 `成功退款 S + 未决退款 P == floor(去重收款 C × 比例 / 10000)`。
- 同一 `payment_id` 只入账一次；同一 `command_id` 终态唯一；失败补发新标识指令，旧指令保留。

## 测试

```sh
python3 -m unittest discover -s tests -v
```
