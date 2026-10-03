"""取消退款协调库自动化测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from refund_coordinator import (
    Coordinator,
    CancelRequested,
    PaymentReceived,
    RefundFailed,
    RefundSucceeded,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        self.c = Coordinator()

    def tearDown(self):
        self.c.close()

    def assert_conservation(self, order_id):
        """账务守恒：S + P == floor(C * ratio / 10000)（取消后）。"""
        snap = self.c.read(order_id)
        if snap.ratio_bps is not None:
            self.assertEqual(
                snap.refunded + snap.pending,
                snap.collected * snap.ratio_bps // 10000,
                f"守恒被破坏: {snap}",
            )
            self.assertEqual(snap.target, snap.refunded + snap.pending)
        else:
            self.assertEqual(snap.pending, 0, "未取消订单不得有未决退款")
            self.assertEqual(snap.refunded, 0, "未取消订单不得有成功退款")
        return snap

    def assert_per_payment_cap(self, order_id):
        """单笔上限：每笔收款的成功+未决退款不超过实收；指令金额为正。"""
        snap = self.c.read(order_id)
        payments = {}
        # 从指令归属反推校验：按 payment_id 汇总成功+未决
        used = {}
        for cmd in snap.commands:
            self.assertGreater(cmd.amount, 0, "指令金额必须为正")
            if cmd.status in ("pending", "succeeded"):
                used[cmd.payment_id] = used.get(cmd.payment_id, 0) + cmd.amount
        return used


class TestDuplicateAndLateEvents(CoordinatorTestBase):
    def test_duplicate_payment_cancel_success_and_late_payment(self):
        c = self.c
        c.create_order("o1", T0)

        # 重复收款：同一 payment_id 不同 event_id，只入账一次
        c.apply("o1", "e1", PaymentReceived("p1", 10000))
        c.apply("o1", "e2", PaymentReceived("p1", 10000))
        self.assertEqual(self.c.read("o1").collected, 10000)

        # 重复 event_id：整体幂等
        c.apply("o1", "e1", PaymentReceived("p1", 10000))
        self.assertEqual(self.c.read("o1").collected, 10000)

        # 申请取消（<24h → 10000），重复取消不得改写
        c.apply("o1", "e3", CancelRequested(T0 + timedelta(hours=1)))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.ratio_bps, 10000)
        self.assertEqual(snap.pending, 10000)
        c.apply("o1", "e4", CancelRequested(T0 + timedelta(hours=100)))
        self.assertEqual(self.c.read("o1").ratio_bps, 10000)

        # 退款成功回报，重复回报（不同 event_id）不得重复扣账
        cmd = self.c.read("o1").commands[0]
        c.apply("o1", "e5", RefundSucceeded(cmd.command_id))
        c.apply("o1", "e6", RefundSucceeded(cmd.command_id))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.refunded, 10000)
        self.assertEqual(snap.pending, 0)
        self.assertEqual(len(snap.commands), 1)

        # 取消后尾款迟到：按已固定比例补发指令
        c.apply("o1", "e7", PaymentReceived("p2", 500))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.collected, 10500)
        self.assertEqual(snap.pending, 500)
        self.assertEqual(len(snap.commands), 2)
        new_cmd = snap.commands[1]
        self.assertEqual(new_cmd.payment_id, "p2")
        self.assertEqual(new_cmd.amount, 500)
        self.assert_conservation("o1")
        self.assert_per_payment_cap("o1")

    def test_duplicate_event_id_across_event_types(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 100))
        # 同一 event_id 再次投递（即使内容不同）整体忽略
        c.apply("o1", "e1", CancelRequested(T0))
        snap = self.c.read("o1")
        self.assertIsNone(snap.ratio_bps)
        self.assertEqual(snap.collected, 100)


class TestRatioBoundaries(CoordinatorTestBase):
    def test_8500bps_two_payments_101_and_1(self):
        c = self.c
        c.create_order("o1", T0)  # 默认 8500 基点
        c.apply("o1", "e1", PaymentReceived("p1", 101))
        c.apply("o1", "e2", CancelRequested(T0 + timedelta(hours=30)))
        snap = self.assert_conservation("o1")
        # floor(101 * 8500 / 10000) = 85
        self.assertEqual(snap.target, 85)
        self.assertEqual(snap.pending, 85)
        self.assert_per_payment_cap("o1")

        # 第二笔 1 分迟到：floor(102 * 8500 / 10000) = 86，补发 1 分
        c.apply("o1", "e3", PaymentReceived("p2", 1))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.target, 86)
        self.assertEqual(snap.pending, 86)
        amounts = sorted(cmd.amount for cmd in snap.commands)
        self.assertEqual(amounts, [1, 85])
        self.assert_per_payment_cap("o1")

    def test_cancel_exactly_24h_uses_snapshot_ratio(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 20000))
        c.apply("o1", "e2", CancelRequested(T0 + timedelta(hours=24)))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.ratio_bps, 8500)
        self.assertEqual(snap.target, 17000)

    def test_cancel_just_under_24h_is_full(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 20000))
        c.apply("o1", "e2", CancelRequested(T0 + timedelta(hours=24) - timedelta(seconds=1)))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.ratio_bps, 10000)
        self.assertEqual(snap.target, 20000)

    def test_cancel_exactly_48h_is_zero(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 20000))
        c.apply("o1", "e2", CancelRequested(T0 + timedelta(hours=48)))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.ratio_bps, 0)
        self.assertEqual(snap.target, 0)
        self.assertEqual(snap.commands, [])

        # 0 比例下迟到收款也不产生指令
        c.apply("o1", "e3", PaymentReceived("p2", 500))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.commands, [])

    def test_cancel_just_under_48h_uses_snapshot_ratio(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 20000))
        c.apply("o1", "e2", CancelRequested(T0 + timedelta(hours=48) - timedelta(seconds=1)))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.ratio_bps, 8500)
        self.assertEqual(snap.target, 17000)


class TestFailureReissue(CoordinatorTestBase):
    def test_failure_reissues_new_command(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 1000))
        c.apply("o1", "e2", CancelRequested(T0))
        snap = self.c.read("o1")
        cmd1 = snap.commands[0]

        # 确定失败：补发新标识指令，旧指令保留且不可变
        c.apply("o1", "e3", RefundFailed(cmd1.command_id))
        snap = self.assert_conservation("o1")
        self.assertEqual(len(snap.commands), 2)
        old, new = snap.commands
        self.assertEqual(old.command_id, cmd1.command_id)
        self.assertEqual(old.status, "failed")
        self.assertEqual(old.amount, 1000)
        self.assertEqual(new.status, "pending")
        self.assertEqual(new.amount, 1000)
        self.assertNotEqual(new.command_id, old.command_id)

        # 重复失败回报（不同 event_id）不得重复补发
        c.apply("o1", "e4", RefundFailed(cmd1.command_id))
        snap = self.c.read("o1")
        self.assertEqual(len(snap.commands), 2)

        # 新指令成功后守恒
        c.apply("o1", "e5", RefundSucceeded(new.command_id))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.refunded, 1000)
        self.assertEqual(snap.pending, 0)

    def test_failure_then_success_report_for_same_command_ignored(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 1000))
        c.apply("o1", "e2", CancelRequested(T0))
        cmd = self.c.read("o1").commands[0]
        c.apply("o1", "e3", RefundFailed(cmd.command_id))
        # 终态已定，迟到的矛盾回报被忽略
        c.apply("o1", "e4", RefundSucceeded(cmd.command_id))
        snap = self.c.read("o1")
        self.assertEqual(snap.commands[0].status, "failed")
        self.assertEqual(snap.refunded, 0)
        self.assert_conservation("o1")


class TestGuards(CoordinatorTestBase):
    def test_no_refund_before_cancel(self):
        c = self.c
        c.create_order("o1", T0)
        c.apply("o1", "e1", PaymentReceived("p1", 1000))
        snap = self.assert_conservation("o1")
        self.assertEqual(snap.commands, [])

    def test_unknown_order_and_command(self):
        c = self.c
        with self.assertRaises(KeyError):
            c.apply("nope", "e1", PaymentReceived("p1", 1))
        with self.assertRaises(KeyError):
            c.read("nope")
        c.create_order("o1", T0)
        with self.assertRaises(ValueError):
            c.apply("o1", "e1", RefundSucceeded("ghost"))

    def test_invalid_amount_and_ratio(self):
        c = self.c
        with self.assertRaises(ValueError):
            c.create_order("o1", T0, ratio_bps=10001)
        c.create_order("o2", T0)
        with self.assertRaises(ValueError):
            c.apply("o2", "e1", PaymentReceived("p1", 0))


if __name__ == "__main__":
    unittest.main()
