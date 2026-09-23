import unittest
from unittest.mock import mock_open, patch

from src.services.listener.browser_recycle import BrowserRecyclePolicy, read_available_memory_mb


class BrowserRecyclePolicyTest(unittest.TestCase):
    def test_reads_linux_available_memory(self):
        meminfo = "MemTotal:       4000000 kB\nMemAvailable:    786432 kB\n"
        with patch("builtins.open", mock_open(read_data=meminfo)):
            self.assertEqual(768.0, read_available_memory_mb())

    def test_recycles_when_interval_is_reached(self):
        now = [100.0]
        policy = BrowserRecyclePolicy(
            interval_seconds=60,
            min_available_memory_mb=0,
            clock=lambda: now[0],
        )
        policy.mark_browser_started()

        now[0] = 160.0

        self.assertIn("interval reached", policy.recycle_reason())

    def test_recycles_on_low_memory_after_minimum_age(self):
        now = [100.0]
        policy = BrowserRecyclePolicy(
            interval_seconds=0,
            min_available_memory_mb=768,
            memory_check_interval_seconds=15,
            memory_recycle_min_age_seconds=600,
            clock=lambda: now[0],
            memory_reader=lambda: 512,
        )
        policy.mark_browser_started()

        now[0] = 699.0
        self.assertIsNone(policy.recycle_reason())
        now[0] = 700.0
        self.assertIn("512 MiB < 768 MiB", policy.recycle_reason())

    def test_memory_checks_are_throttled(self):
        now = [100.0]
        readings = [1024, 512]
        policy = BrowserRecyclePolicy(
            interval_seconds=0,
            min_available_memory_mb=768,
            memory_check_interval_seconds=15,
            memory_recycle_min_age_seconds=0,
            clock=lambda: now[0],
            memory_reader=lambda: readings.pop(0),
        )
        policy.mark_browser_started()

        now[0] = 101.0
        self.assertIsNone(policy.recycle_reason())
        now[0] = 110.0
        self.assertIsNone(policy.recycle_reason())
        self.assertEqual([512], readings)
        now[0] = 116.0
        self.assertIsNotNone(policy.recycle_reason())


if __name__ == "__main__":
    unittest.main()
