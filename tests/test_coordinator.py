"""取消退款协调库测试：账务守恒、单笔上限、幂等性。"""
import unittest

from refund_coordinator import Coordinator, Event, EventType

T0 = "2026-01-01T00:00:00+00:00"


def pay(eid, pid, amount):
    return Event(event_id=eid, type=EventType.PAYMENT_SUCCEEDED,
                 payment_id=pid, amount=amount)


def cancel(eid, at):
    return Event(event_id=eid, type=EventType.CANCEL_REQUESTED, requested_at=at)


def ok(eid, cid):
    return Event(event_id=eid, type=EventType.REFUND_SUCCEEDED, command_id=cid)


def fail(eid, cid):
    return Event(event_id=eid, type=EventType.REFUND_FAILED, command_id=cid)


class InvariantsMixin:
    def assert_conservation(self, c, order_id):
        snap, _ = c.read(order_id)
        if snap.cancelled:
            self.assertEqual(
                snap.refunded_total + snap.pending_total,
                snap.collected_total * snap.ratio_bps // 10000,
                "S + P 必须等于 floor(C * ratio / 10000)",
            )
            self.assertEqual(snap.target_total,
                             snap.collected_total * snap.ratio_bps // 10000)
        else:
            self.assertEqual(snap.refunded_total + snap.pending_total, 0)

    def assert_per_payment_cap(self, c, order_id):
        _, cmds = c.read(order_id)
        committed = {}
        for cmd in cmds:
            self.assertGreater(cmd.amount, 0, "指令金额必须为正")
            if cmd.status in ("pending", "succeeded"):
                committed[cmd.payment_id] = committed.get(cmd.payment_id, 0) + cmd.amount
        for pid, total in committed.items():
            received = c.store.conn.execute(
                "SELECT amount FROM payments WHERE order_id = ? AND payment_id = ?",
                (order_id, pid)).fetchone()[0]
            self.assertLessEqual(total, received,
                                 f"{pid} 成功+未决退款不得超过实收")


class TestCoordinator(InvariantsMixin, unittest.TestCase):
    def setUp(self):
        self.c = Coordinator()

    def test_no_refund_without_cancel(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 500))
        snap, cmds = self.c.read("o1")
        self.assertFalse(snap.cancelled)
        self.assertEqual(cmds, [])
        self.assert_conservation(self.c, "o1")

    def test_duplicate_payment_credited_once(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 101))
        self.c.apply("o1", pay("e2", "p1", 101))  # 同 payment_id 不同 event_id
        self.c.apply("o1", pay("e1", "p1", 101))  # 同 event_id 重复投递
        snap, _ = self.c.read("o1")
        self.assertEqual(snap.collected_total, 101)

    def test_duplicate_event_id_idempotent(self):
        self.c.create_order("o1", T0)
        ev = cancel("e1", "2026-01-01T01:00:00+00:00")
        self.c.apply("o1", pay("e0", "p1", 200))
        self.c.apply("o1", ev)
        _, cmds1 = self.c.read("o1")
        self.c.apply("o1", ev)  # 重复投递同一取消事件
        _, cmds2 = self.c.read("o1")
        self.assertEqual(cmds1, cmds2)
        self.assert_conservation(self.c, "o1")

    def test_first_cancel_fixes_ratio_later_cannot_rewrite(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 10000))
        self.c.apply("o1", cancel("e2", "2026-01-03T00:00:00+00:00"))  # 满48h → 0
        self.c.apply("o1", cancel("e3", "2026-01-01T00:00:00+00:00"))  # 试图改写为全退
        snap, cmds = self.c.read("o1")
        self.assertEqual(snap.ratio_bps, 0)
        self.assertEqual(cmds, [])

    def test_refund_success_and_late_payment_after_cancel(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 101))
        self.c.apply("o1", cancel("e2", "2026-01-01T12:00:00+00:00"))  # <24h → 全退
        snap, cmds = self.c.read("o1")
        self.assertEqual(snap.ratio_bps, 10000)
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].amount, 101)
        self.assertEqual(cmds[0].payment_id, "p1")

        # 重复成功回报（不同 event_id）不得重复扣账
        self.c.apply("o1", ok("e3", cmds[0].command_id))
        self.c.apply("o1", ok("e4", cmds[0].command_id))
        snap, _ = self.c.read("o1")
        self.assertEqual(snap.refunded_total, 101)
        self.assertEqual(snap.pending_total, 0)

        # 取消后尾款迟到 → 补发新指令
        self.c.apply("o1", pay("e5", "p2", 1))
        snap, cmds = self.c.read("o1")
        self.assertEqual(snap.collected_total, 102)
        self.assertEqual(snap.pending_total, 1)
        pending = [c for c in cmds if c.status == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].payment_id, "p2")
        self.assertNotEqual(pending[0].command_id, cmds[0].command_id)
        self.assert_conservation(self.c, "o1")
        self.assert_per_payment_cap(self.c, "o1")

    def test_ratio_8500_two_payments_101_and_1(self):
        self.c.create_order("o1", T0)  # 默认 8500 基点
        self.c.apply("o1", pay("e1", "p1", 101))
        self.c.apply("o1", cancel("e2", "2026-01-02T00:00:00+00:00"))  # 恰满24h → r
        snap, cmds = self.c.read("o1")
        self.assertEqual(snap.ratio_bps, 8500)
        # floor(101 * 8500 / 10000) = 85
        self.assertEqual(snap.target_total, 85)
        self.assertEqual(sum(c.amount for c in cmds), 85)

        # 先后到账的 1 分尾款：floor(102 * 8500 / 10000) = 86 → 补发 1
        self.c.apply("o1", pay("e3", "p2", 1))
        snap, cmds = self.c.read("o1")
        self.assertEqual(snap.target_total, 86)
        self.assertEqual(snap.pending_total, 86)
        self.assert_conservation(self.c, "o1")
        self.assert_per_payment_cap(self.c, "o1")

    def test_cancel_boundaries_24h_and_48h(self):
        # 恰满 24 小时 → 中间档 r（不足 24h 才是 10000）
        self.c.create_order("o24", T0)
        self.c.apply("o24", cancel("e1", "2026-01-02T00:00:00+00:00"))
        snap, _ = self.c.read("o24")
        self.assertEqual(snap.ratio_bps, 8500)

        # 恰满 48 小时 → 0（不足 48h 才是 r）
        self.c.create_order("o48", T0)
        self.c.apply("o48", cancel("e2", "2026-01-03T00:00:00+00:00"))
        snap, _ = self.c.read("o48")
        self.assertEqual(snap.ratio_bps, 0)

        # 差 1 秒满 24 小时 → 10000
        self.c.create_order("o23", T0)
        self.c.apply("o23", cancel("e3", "2026-01-01T23:59:59+00:00"))
        snap, _ = self.c.read("o23")
        self.assertEqual(snap.ratio_bps, 10000)

    def test_failure_reissue_and_duplicate_failure_idempotent(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 100))
        self.c.apply("o1", cancel("e2", "2026-01-01T01:00:00+00:00"))
        _, cmds = self.c.read("o1")
        cmd1 = cmds[0].command_id

        # 确定失败 → 补发新指令，旧指令保留
        self.c.apply("o1", fail("e3", cmd1))
        snap, cmds = self.c.read("o1")
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0].status, "failed")
        self.assertEqual(cmds[1].status, "pending")
        self.assertEqual(cmds[1].amount, 100)
        self.assertNotEqual(cmds[1].command_id, cmd1)
        self.assert_conservation(self.c, "o1")

        # 重复失败回报（不同 event_id）不得重复补发
        self.c.apply("o1", fail("e4", cmd1))
        _, cmds = self.c.read("o1")
        self.assertEqual(len(cmds), 2)

        # 新指令成功 → 守恒仍成立
        self.c.apply("o1", ok("e5", cmds[1].command_id))
        snap, _ = self.c.read("o1")
        self.assertEqual(snap.refunded_total, 100)
        self.assertEqual(snap.pending_total, 0)
        self.assert_conservation(self.c, "o1")
        self.assert_per_payment_cap(self.c, "o1")

    def test_commands_immutable_and_persistent(self):
        self.c.create_order("o1", T0)
        self.c.apply("o1", pay("e1", "p1", 50))
        self.c.apply("o1", cancel("e2", "2026-01-01T01:00:00+00:00"))
        _, cmds1 = self.c.read("o1")
        self.c.apply("o1", ok("e3", cmds1[0].command_id))
        _, cmds2 = self.c.read("o1")
        # 指令不删除，标识/归属/金额不变，仅状态推进
        self.assertEqual(len(cmds2), 1)
        self.assertEqual(cmds2[0].command_id, cmds1[0].command_id)
        self.assertEqual(cmds2[0].payment_id, cmds1[0].payment_id)
        self.assertEqual(cmds2[0].amount, cmds1[0].amount)
        self.assertEqual(cmds2[0].status, "succeeded")


if __name__ == "__main__":
    unittest.main()
