"""Contract tests for the Windscribe API port and session migration.

The fake API follows upstream ca66479's request/response contract. No test
contacts Windscribe or rotates an actual account's port.
"""

import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

os.environ.setdefault("WS_USERNAME", "test-user")
os.environ.setdefault("WS_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import config
from ws.cookie import SESSION_KEY, load_token
from ws.ws import Windscribe, WindscribeError


class FakeApi:
    """Track requests and model accepted, expired, and rejected API sessions."""

    def __init__(self, port=None):
        self.port = port
        self.new_port = (49152, 49152)
        self.token = "accepted-session"
        self.requests = []
        self.session_error = None
        self.port_responses = []
        self.delete_success = 1

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def calls(self):
        return [(r.method, r.url.path) for r in self.requests]

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.windscribe.com"
        if request.url.path == "/AuthToken/login":
            return httpx.Response(
                200, json={"data": {"token": "one-use", "token_sig": "signature"}}
            )
        if request.url.path == "/Session":
            if self.session_error:
                return httpx.Response(403, json=self.session_error)
            return httpx.Response(201, json={"data": {"session_auth_hash": self.token}})
        if self.port_responses:
            return self.port_responses.pop(0)
        if request.headers["authorization"] != f"Bearer {self.token}":
            return httpx.Response(403, json={"errorCode": 701})
        if request.method != "GET":
            form = parse_qs(request.content.decode())
            if form.get("session_auth_hash") != [self.token]:
                return httpx.Response(403, json={"errorCode": 701})
        if request.method == "GET":
            epf = {"ext": str(self.port), "int": str(self.port)} if self.port else False
            return httpx.Response(200, json={"data": {"success": 1, "epf": epf}})
        if request.method == "DELETE":
            self.port = None
            return httpx.Response(200, json={"data": {"success": self.delete_success}})
        external, internal = self.new_port
        self.port = external
        return httpx.Response(
            200,
            json={"data": {"success": 1, "epf": {"ext": external, "int": internal}}},
        )


class WindscribeApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.session = self.directory / "cookie.json"
        self.legacy = self.directory / "cookie.pkl"
        self.browser = self.directory / "browser.json"
        for name, value in (
            ("WS_COOKIE_JSON", self.session),
            ("WS_COOKIE", self.legacy),
            ("WS_COOKIE_IMPORT_FILE", None),
        ):
            self.enterContext(patch.object(config, name, value))
        self.api = FakeApi()

    def saved_session(self, token="accepted-session", path=None):
        (path or self.session).write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": SESSION_KEY,
                            "value": token,
                            "domain": ".windscribe.com",
                            "path": "/",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def run_setup(self, totp=None):
        with Windscribe("user", "password", totp, client=self.api.client()) as ws:
            return ws.setup()

    def test_fresh_api_login_then_read_and_create_port(self):
        self.assertEqual(self.run_setup(), 49152)
        self.assertEqual(
            self.api.calls(),
            [
                ("POST", "/AuthToken/login"),
                ("POST", "/Session"),
                ("GET", "/EphPortForwarding"),
                ("POST", "/EphPortForwarding"),
            ],
        )
        auth, session = self.api.requests[:2]
        self.assertEqual(
            json.loads(auth.content), {"username": "user", "password": "password"}
        )
        self.assertEqual(
            json.loads(session.content),
            {
                "username": "user",
                "password": "password",
                "session_type_id": 1,
                "platform": "legacy-web",
                "secure_token": "one-use",
                "secure_token_sig": "signature",
            },
        )
        self.assertEqual(auth.headers["authorization"], "Bearer anonymous")
        self.assertEqual(session.headers["authorization"], "Bearer anonymous")
        self.assertEqual(load_token().token, self.api.token)

    def test_existing_json_token_does_not_login(self):
        self.saved_session()
        self.assertEqual(self.run_setup(), 49152)
        self.assertEqual(
            self.api.calls(),
            [("GET", "/EphPortForwarding"), ("POST", "/EphPortForwarding")],
        )

    def test_legacy_pickle_is_migrated_after_api_accepts_token(self):
        self.legacy.write_bytes(
            pickle.dumps({SESSION_KEY: self.api.token, "ref": "old"})
        )
        self.assertEqual(self.run_setup(), 49152)
        self.assertTrue(self.legacy.exists())
        self.assertEqual(load_token().source, "session")
        self.assertEqual(load_token().token, self.api.token)
        self.assertEqual(self.api.calls()[0], ("GET", "/EphPortForwarding"))

    def test_browser_export_token_is_used_and_migrated(self):
        self.saved_session(path=self.browser)
        with patch.object(config, "WS_COOKIE_IMPORT_FILE", self.browser):
            self.assertEqual(self.run_setup(), 49152)
        self.assertEqual(load_token().token, self.api.token)
        self.assertEqual(len(self.api.calls()), 2)

    def test_stale_token_logs_in_once_and_uses_new_token_in_forms(self):
        self.saved_session("expired")
        self.api.port = 40000
        self.assertEqual(self.run_setup(), 49152)
        self.assertEqual(
            self.api.calls(),
            [
                ("GET", "/EphPortForwarding"),
                ("POST", "/AuthToken/login"),
                ("POST", "/Session"),
                ("GET", "/EphPortForwarding"),
                ("DELETE", "/EphPortForwarding"),
                ("POST", "/EphPortForwarding"),
            ],
        )
        deletion = self.api.requests[-2]
        self.assertEqual(
            parse_qs(deletion.content.decode()),
            {"id": ["40000"], "session_auth_hash": [self.api.token]},
        )
        self.assertEqual(load_token().token, self.api.token)

    def test_token_expiring_on_delete_retries_with_new_header_and_form(self):
        self.saved_session("expired")
        self.api.port_responses = [
            httpx.Response(
                200,
                json={"data": {"success": 1, "epf": {"ext": "40000", "int": "40000"}}},
            )
        ]
        self.assertEqual(self.run_setup(), 49152)
        self.assertEqual(
            self.api.calls(),
            [
                ("GET", "/EphPortForwarding"),
                ("DELETE", "/EphPortForwarding"),
                ("POST", "/AuthToken/login"),
                ("POST", "/Session"),
                ("DELETE", "/EphPortForwarding"),
                ("POST", "/EphPortForwarding"),
            ],
        )
        for request in self.api.requests[-2:]:
            self.assertEqual(
                request.headers["authorization"], f"Bearer {self.api.token}"
            )
            self.assertEqual(
                parse_qs(request.content.decode())["session_auth_hash"],
                [self.api.token],
            )

    def test_rejected_login_never_overwrites_old_session(self):
        self.saved_session("expired")
        original = self.session.read_bytes()
        self.api.session_error = {
            "errorCode": 702,
            "errorDescription": "do not log private details",
        }
        with self.assertRaisesRegex(WindscribeError, "WS_PASSWORD") as error:
            self.run_setup()
        self.assertNotIn("private details", str(error.exception))
        self.assertEqual(self.session.read_bytes(), original)
        self.assertEqual(len(self.api.calls()), 3)

    def test_new_session_rejected_does_not_loop_or_persist(self):
        self.api.port_responses = [httpx.Response(403, json={"errorCode": 701})]
        with self.assertRaises(WindscribeError):
            self.run_setup()
        self.assertFalse(self.session.exists())
        self.assertEqual(len(self.api.calls()), 3)

    def test_retry_after_cached_session_rejection_is_bounded(self):
        self.saved_session("expired")
        self.api.port_responses = [
            httpx.Response(403, json={"errorCode": 701}),
            httpx.Response(403, json={"errorCode": 701}),
        ]
        with self.assertRaises(WindscribeError):
            self.run_setup()
        self.assertEqual(len(self.api.calls()), 4)
        self.assertEqual(load_token().token, "expired")

    def test_other_api_errors_do_not_trigger_login_or_port_mutations(self):
        self.saved_session()
        self.api.port_responses = [httpx.Response(403, json={"errorCode": 999})]
        with self.assertRaises(WindscribeError):
            self.run_setup()
        self.assertEqual(self.api.calls(), [("GET", "/EphPortForwarding")])

    def test_totp_is_sent_as_current_six_digit_code(self):
        self.run_setup("JBSWY3DPEHPK3PXP")
        payload = json.loads(self.api.requests[1].content)
        self.assertEqual(len(payload["2fa_code"]), 6)
        self.assertTrue(payload["2fa_code"].isdigit())

    def test_2fa_error_explains_secret_requirement(self):
        self.api.session_error = {"errorCode": 1340}
        with self.assertRaisesRegex(WindscribeError, "WS_TOTP"):
            self.run_setup()
        self.assertFalse(self.session.exists())

    def test_cache_write_failure_does_not_stop_rotation(self):
        with patch("ws.ws.save_token", side_effect=OSError("read-only")):
            with self.assertLogs("Windscribe", level="WARNING"):
                self.assertEqual(self.run_setup(), 49152)

    def test_api_response_shapes_and_http_failures_abort_before_mutation(self):
        self.saved_session()
        for response in (
            httpx.Response(403, text="<html>challenge</html>"),
            httpx.Response(500, json={"data": {"success": 1, "epf": False}}),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": {"success": 1}}),
            httpx.Response(200, json={"data": {"success": 1, "epf": []}}),
        ):
            with self.subTest(response=response):
                self.api.requests = []
                self.api.port_responses = [response]
                with self.assertRaises(WindscribeError):
                    self.run_setup()
                self.assertEqual(len(self.api.calls()), 1)

    def test_failed_delete_prevents_requesting_new_port(self):
        self.saved_session()
        self.api.port = 40000
        self.api.delete_success = 0
        with self.assertRaisesRegex(WindscribeError, "delete"):
            self.run_setup()
        self.assertEqual(self.api.calls()[-1], ("DELETE", "/EphPortForwarding"))

    def test_mismatched_or_invalid_ports_are_rejected(self):
        self.saved_session()
        for ports in ((49152, 49153), (0, 0), (65536, 65536), (True, True)):
            with self.subTest(ports=ports):
                self.api.port = None
                self.api.new_port = ports
                with self.assertRaises(WindscribeError):
                    self.run_setup()


if __name__ == "__main__":
    unittest.main()
