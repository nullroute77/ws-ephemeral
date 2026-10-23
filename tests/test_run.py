"""Verify API-assigned ports still reach Gluetun, qBittorrent, and the monitor."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WS_USERNAME", "test-user")
os.environ.setdefault("WS_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

with patch("logger.setup_logging"):
    import run


class PortHandoffTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("qbit_found", True),
            ("GLUETUN_URL", "http://gluetun:8000"),
            ("GLUETUN_API_KEY", "gluetun-test-key"),
            ("QBIT_API_KEY", "qbit-test-key"),
            ("QBIT_USERNAME", None),
            ("QBIT_PASSWORD", None),
        ):
            self.enterContext(patch.object(run.config, name, value))
        self.enterContext(patch.object(run.service_monitor, "HEARTBEAT", True))
        self.enterContext(patch.object(run.service_monitor, "_DESIRED_PORT", None))
        self.windscribe = self.enterContext(patch("run.Windscribe"))
        self.gluetun = self.enterContext(patch("run.GluetunManager"))
        self.qbit = self.enterContext(patch("run.QbitManager"))

    def test_new_port_reaches_firewall_qbit_and_monitor(self):
        self.windscribe.return_value.__enter__.return_value.setup.return_value = 49152

        run.main()

        self.assertEqual(run.service_monitor._DESIRED_PORT, 49152)
        self.gluetun.return_value.__enter__.return_value.set_forwarded_port.assert_called_once_with(
            49152
        )
        self.qbit.return_value.set_listen_port.assert_called_once_with(49152)
        self.assertEqual(self.qbit.call_args.kwargs["api_key"], "qbit-test-key")
        self.assertEqual(self.gluetun.call_args.kwargs["api_key"], "gluetun-test-key")

    def test_api_failure_does_not_change_firewall_or_qbit(self):
        self.windscribe.return_value.__enter__.return_value.setup.side_effect = (
            ValueError("API rejected")
        )

        with self.assertLogs(level="ERROR"):
            run.main()

        self.assertIsNone(run.service_monitor._DESIRED_PORT)
        self.gluetun.assert_not_called()
        self.qbit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
