"""Tests for JSON cookie persistence and browser cookie imports."""

import json
import os
import pickle
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("WS_USERNAME", "test-user")
os.environ.setdefault("WS_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import config
from ws.cookie import SESSION_KEY, load_cookie, load_token, save_cookie, save_token


class CookieStorageTests(unittest.TestCase):
    """Exercise browser imports, JSON storage, and legacy migration reads."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.session_file = self.directory / "cookie.json"
        self.legacy_file = self.directory / "cookie.pkl"
        self.import_file = self.directory / "browser.json"
        self.config_patches = (
            patch.object(config, "WS_COOKIE_JSON", self.session_file),
            patch.object(config, "WS_COOKIE", self.legacy_file),
            patch.object(config, "WS_COOKIE_IMPORT_FILE", self.import_file),
        )
        for config_patch in self.config_patches:
            config_patch.start()

    def tearDown(self) -> None:
        for config_patch in reversed(self.config_patches):
            config_patch.stop()
        self.temporary_directory.cleanup()

    def write_import(self, payload: object) -> None:
        self.import_file.write_text(json.dumps(payload), encoding="utf-8")

    def test_imports_windscribe_browser_cookies_and_filters_other_domains(self) -> None:
        self.write_import(
            [
                {
                    "name": "ws_session",
                    "value": "secret",
                    "domain": ".windscribe.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "expirationDate": 2_000_000_000,
                },
                {
                    "name": "unrelated",
                    "value": "discard-me",
                    "domain": ".example.com",
                    "path": "/",
                },
            ]
        )

        loaded = load_cookie()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.source, "browser-import")
        stored = list(loaded.cookies.jar)
        self.assertEqual([cookie.name for cookie in stored], ["ws_session"])
        self.assertEqual(stored[0].domain, ".windscribe.com")
        self.assertTrue(stored[0].secure)
        self.assertEqual(stored[0].expires, 2_000_000_000)

    def test_imports_browser_storage_state_object(self) -> None:
        self.write_import(
            {
                "cookies": [
                    {
                        "name": "ws_session",
                        "value": "secret",
                        "domain": "windscribe.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        )

        loaded = load_cookie()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.source, "browser-import")
        self.assertEqual(list(loaded.cookies.jar)[0].value, "secret")

    def test_rejects_export_without_windscribe_cookies(self) -> None:
        self.write_import(
            [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".example.com",
                    "path": "/",
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "no Windscribe cookies"):
            load_cookie()

    def test_saves_and_reloads_session_as_json(self) -> None:
        self.write_import(
            [
                {
                    "name": "ws_session",
                    "value": "secret",
                    "domain": ".windscribe.com",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        imported = load_cookie()
        assert imported is not None

        save_cookie(imported.cookies)
        loaded = load_cookie()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.source, "session")
        self.assertEqual(list(loaded.cookies.jar)[0].value, "secret")
        self.assertEqual(self.session_file.stat().st_mode & 0o777, 0o600)
        payload = json.loads(self.session_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)

    def test_reads_legacy_pickle_when_no_json_session_exists(self) -> None:
        with open(self.legacy_file, "wb") as legacy_file:
            pickle.dump({"ws_session": "legacy-secret"}, legacy_file)

        loaded = load_cookie()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.source, "legacy")
        self.assertEqual(loaded.cookies.get("ws_session"), "legacy-secret")

    def test_api_token_roundtrip_uses_private_json_file(self) -> None:
        save_token("accepted-token")

        loaded = load_token()

        self.assertEqual(loaded.token, "accepted-token")
        self.assertEqual(loaded.source, "session")
        self.assertEqual(self.session_file.stat().st_mode & 0o777, 0o600)
        cookie = json.loads(self.session_file.read_text())["cookies"][0]
        self.assertEqual(cookie["name"], SESSION_KEY)
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httpOnly"])
        self.assertEqual(list(self.directory.glob(".cookie-*.tmp")), [])

    def test_conflicting_session_tokens_are_not_guessed(self) -> None:
        self.write_import(
            [
                {"name": SESSION_KEY, "value": "one", "domain": ".windscribe.com"},
                {"name": SESSION_KEY, "value": "two", "domain": "windscribe.com"},
            ]
        )
        self.assertIsNone(load_token())

    def test_non_windscribe_domain_cannot_supply_token(self) -> None:
        self.session_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": SESSION_KEY,
                            "value": "wrong",
                            "domain": "evilwindscribe.com",
                        }
                    ]
                }
            )
        )
        self.assertIsNone(load_token())

    def test_corrupt_saved_cache_allows_fresh_login_without_deleting_file(self) -> None:
        self.session_file.write_text("not json")
        self.assertIsNone(load_token())
        self.assertTrue(self.session_file.exists())

    def test_legacy_cache_rejects_pickle_globals(self) -> None:
        self.legacy_file.write_bytes(pickle.dumps(Path("unused")))
        with self.assertRaises(pickle.UnpicklingError):
            load_cookie()
        self.assertIsNone(load_token())

    def test_missing_browser_import_reports_file_path(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "WS_COOKIE_IMPORT_FILE"):
            load_token()


if __name__ == "__main__":
    unittest.main()
