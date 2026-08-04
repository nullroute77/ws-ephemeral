"""Tests for periodic qBittorrent and Gluetun monitoring."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WS_USERNAME", "test-user")
os.environ.setdefault("WS_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import monitor


class MonitorTests(unittest.TestCase):
    """Exercise desired-port state and Gluetun reconciliation."""

    def setUp(self) -> None:
        monitor._DESIRED_PORT = None
        monitor.HEARTBEAT = True

    def test_records_valid_desired_port(self) -> None:
        monitor.set_desired_port(49152)

        self.assertEqual(monitor._DESIRED_PORT, 49152)

    def test_rejects_invalid_desired_port(self) -> None:
        for port in (0, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                monitor.set_desired_port(port)

    @patch("monitor.GluetunManager")
    def test_skips_update_when_gluetun_matches(self, manager_constructor) -> None:
        monitor.set_desired_port(49152)
        gluetun = manager_constructor.return_value.__enter__.return_value
        gluetun.get_forwarded_ports.return_value = [49152]

        with patch.object(monitor.config, "GLUETUN_URL", "http://gluetun:8000"):
            self.assertTrue(monitor.reconcile_gluetun())

        gluetun.set_forwarded_port.assert_not_called()

    @patch("monitor.GluetunManager")
    def test_repairs_mismatched_gluetun_port(self, manager_constructor) -> None:
        monitor.set_desired_port(49152)
        gluetun = manager_constructor.return_value.__enter__.return_value
        gluetun.get_forwarded_ports.return_value = [40000]
        gluetun.set_forwarded_port.return_value = True

        with patch.object(monitor.config, "GLUETUN_URL", "http://gluetun:8000"):
            self.assertTrue(monitor.reconcile_gluetun())

        gluetun.set_forwarded_port.assert_called_once_with(49152)

    @patch("monitor.GluetunManager")
    def test_retries_after_gluetun_error(self, manager_constructor) -> None:
        monitor.set_desired_port(49152)
        gluetun = manager_constructor.return_value.__enter__.return_value
        gluetun.get_forwarded_ports.side_effect = RuntimeError("not ready")

        with (
            patch.object(monitor.config, "GLUETUN_URL", "http://gluetun:8000"),
            self.assertLogs("main.monitor", level="ERROR"),
        ):
            self.assertFalse(monitor.reconcile_gluetun())

    @patch("monitor.reconcile_gluetun")
    @patch("monitor.QbitManager")
    def test_monitor_reconciles_when_qbit_is_unavailable(
        self, qbit_constructor, reconcile_gluetun
    ) -> None:
        qbit_constructor.side_effect = RuntimeError("not ready")

        with self.assertLogs("main.monitor", level="ERROR"):
            self.assertFalse(monitor.monitor())

        reconcile_gluetun.assert_called_once_with()
        self.assertFalse(monitor.HEARTBEAT)


if __name__ == "__main__":
    unittest.main()
