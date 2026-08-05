"""Tests for Windscribe session validation and login fallback behavior."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

os.environ.setdefault("WS_USERNAME", "test-user")
os.environ.setdefault("WS_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from ws.cookie import LoadedCookies
from ws.ws import Windscribe, WindscribeAuthenticationError


def response(
    *,
    status: int = 200,
    text: str = "",
    json_body: object | None = None,
) -> MagicMock:
    """Build the subset of an HTTPX response used by Windscribe."""
    result = MagicMock()
    result.status_code = status
    result.is_success = 200 <= status < 300
    result.text = text
    result.headers = {"content-type": "text/html; charset=utf-8"}
    result.json.return_value = json_body
    return result


def account_page(time: int = 123, token: str = "csrf-token") -> MagicMock:
    return response(text=f"csrf_time = {time}; csrf_token = '{token}'")


class WindscribeAuthenticationTests(unittest.TestCase):
    """Ensure sessions are validated before they are persisted or used."""

    def client_constructor(self, client: MagicMock):
        def construct(**kwargs):
            client.cookies = kwargs["cookies"]
            return client

        return construct

    def test_valid_browser_import_is_persisted_and_used(self) -> None:
        imported = httpx.Cookies()
        imported.set("ws_session", "secret", domain=".windscribe.com")
        client = MagicMock()
        client.post.side_effect = [
            response(json_body={"csrf_time": 1, "csrf_token": "initial"}),
            response(json_body={"success": 1}),
            response(
                json_body={
                    "success": 1,
                    "epf": {"ext": 49152, "int": 49152},
                }
            ),
        ]
        client.get.return_value = account_page()

        with (
            patch(
                "ws.ws.load_cookie",
                return_value=LoadedCookies(imported, "browser-import"),
            ),
            patch("ws.ws.httpx.Client", side_effect=self.client_constructor(client)),
            patch("ws.ws.save_cookie") as save,
        ):
            with Windscribe("user", "password") as windscribe:
                self.assertEqual(windscribe.setup(), 49152)

        save.assert_called_once_with(client.cookies)
        self.assertEqual(client.get.call_count, 1)
        self.assertEqual(client.post.call_count, 3)

    def test_rejected_import_falls_back_to_one_validated_login(self) -> None:
        imported = httpx.Cookies()
        imported.set("ws_session", "expired", domain=".windscribe.com")
        client = MagicMock()
        client.post.side_effect = [
            response(json_body={"csrf_time": 1, "csrf_token": "initial"}),
            response(),
            response(json_body={"success": 1}),
            response(
                json_body={
                    "success": 1,
                    "epf": {"ext": 49152, "int": 49152},
                }
            ),
        ]
        client.get.side_effect = [response(text="login page"), account_page()]

        with (
            patch(
                "ws.ws.load_cookie",
                return_value=LoadedCookies(imported, "browser-import"),
            ),
            patch("ws.ws.httpx.Client", side_effect=self.client_constructor(client)),
            patch("ws.ws.save_cookie") as save,
        ):
            with Windscribe("user", "password") as windscribe:
                self.assertEqual(windscribe.setup(), 49152)

        save.assert_called_once_with(client.cookies)
        self.assertEqual(client.get.call_count, 2)
        self.assertEqual(client.post.call_count, 4)

    def test_rejected_login_is_not_saved(self) -> None:
        client = MagicMock()
        client.post.side_effect = [
            response(json_body={"csrf_time": 1, "csrf_token": "initial"}),
            response(),
        ]
        client.get.return_value = response(text="challenge page")

        with (
            patch("ws.ws.load_cookie", return_value=None),
            patch("ws.ws.httpx.Client", side_effect=self.client_constructor(client)),
            patch("ws.ws.save_cookie") as save,
            Windscribe("user", "password") as windscribe,
            self.assertRaisesRegex(
                WindscribeAuthenticationError,
                "anti-automation challenge",
            ),
        ):
            windscribe.setup()

        save.assert_not_called()
        self.assertFalse(windscribe.is_authenticated)


if __name__ == "__main__":
    unittest.main()
