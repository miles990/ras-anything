import unittest

from ras_anything import faults


class FaultScheduleTest(unittest.TestCase):
    keys = ["GET http://api/v1/forecast?city=%d" % i for i in range(40)]

    def test_same_seed_same_schedule(self):
        a = faults.schedule(faults.Seeded(5, 0.3, 0.1), self.keys)
        b = faults.schedule(faults.Seeded(5, 0.3, 0.1), self.keys)
        self.assertEqual(a, b)

    def test_different_seed_differs(self):
        a = faults.schedule(faults.Seeded(5, 0.3), self.keys)
        b = faults.schedule(faults.Seeded(6, 0.3), self.keys)
        self.assertNotEqual(a, b)

    def test_rate_is_respected_roughly(self):
        s = faults.schedule(faults.Seeded(1, 0.3), self.keys * 10, attempts=5)
        flat = [x for row in s for x in row]
        frac = sum(x is not None for x in flat) / len(flat)
        self.assertTrue(0.2 < frac < 0.4, frac)
        self.assertTrue(set(flat) - {None} <= set(faults.KINDS))

    def test_first_attempt_and_outage(self):
        self.assertEqual(faults.schedule(faults.FirstAttempt("http5xx"), ["k"], 3), [["http5xx", None, None]])
        self.assertEqual(faults.schedule(faults.FirstAttempt("outage"), ["k"], 5),
                         [["refused"] * faults.OUTAGE_K + [None] * (5 - faults.OUTAGE_K)])

    def test_virtual_clock(self):
        import time
        c = faults.VirtualClock()
        c.install()
        try:
            t0 = time.monotonic()
            time.sleep(30)
            self.assertAlmostEqual(time.monotonic() - t0, 30)
            with self.assertRaises(faults.ChaosHang):
                c.hang()
        finally:
            c.uninstall()
        self.assertFalse(issubclass(faults.ChaosHang, Exception))


if __name__ == "__main__":
    unittest.main()
