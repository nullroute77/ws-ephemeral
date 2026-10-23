"""Windscribe API client, adapted from upstream's October 2026 API migration."""

import binascii
import logging
from types import TracebackType
from typing import Any, final

import httpx
import pyotp

import config

from .cookie import CookieSource, load_token, save_token

_SESSION_ERROR_CODES = {500, 701}
_LOGIN_HINTS = {
    702: "check WS_USERNAME, WS_PASSWORD and WS_TOTP",
    1340: "this account uses 2FA; set WS_TOTP to its permanent Base32 secret",
}


class WindscribeError(ValueError):
    """A rejected request or unexpected Windscribe API response."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"Windscribe API [{code}]: {message}")
        self.code = code


@final
class Windscribe:
    """Rotate a matching ephemeral port using an API session token.

    Saved browser-cookie tokens are tried before a new credential login. A
    rejected cached token permits one login and one retry, without scraping
    Windscribe's website or its CSRF markup.
    """

    def __init__(
        self,
        username: str,
        password: str,
        totp: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        loaded = load_token()
        self.token = loaded.token if loaded else None
        self._cookie_source: CookieSource | None = loaded.source if loaded else None
        self._token_validated = False
        self.client = client or httpx.Client(
            headers={
                "origin": config.BASE_URL.rstrip("/"),
                "referer": config.BASE_URL,
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/104.0.0.0 Safari/537.36"
                ),
            },
            timeout=config.REQUEST_TIMEOUT,
        )
        self.username = username
        self.password = password
        self.totp = totp

    def __enter__(self) -> "Windscribe":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _data(response: httpx.Response) -> dict[str, Any]:
        """Decode API errors without logging credentials or response bodies."""
        try:
            body = response.json()
        except ValueError as exc:
            raise WindscribeError(
                None, f"unexpected non-JSON response (HTTP {response.status_code})"
            ) from exc

        if not isinstance(body, dict):
            raise WindscribeError(None, "expected an API response object")
        if "errorCode" in body:
            code = body["errorCode"]
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            code = (
                code if isinstance(code, int) and not isinstance(code, bool) else None
            )
            # Provider messages may contain account data; report a safe diagnostic.
            hint = _LOGIN_HINTS.get(code) if code is not None else None
            raise WindscribeError(
                code, hint or f"request rejected (HTTP {response.status_code})"
            )
        if not response.is_success:
            raise WindscribeError(None, f"request failed (HTTP {response.status_code})")
        data = body.get("data")
        if not isinstance(data, dict):
            raise WindscribeError(None, "API response is missing its data object")
        return data

    def _persist_token(self) -> None:
        if self.token is None:
            return
        try:
            save_token(self.token)
        except OSError:
            self.logger.warning(
                "Could not save cookie.json; the accepted session is usable for "
                "this run but cannot be reused on the next run"
            )
        else:
            if self._cookie_source == "browser-import":
                self.logger.info(
                    "Browser session token accepted by Windscribe API and saved "
                    "as cookie.json"
                )
            elif self._cookie_source == "legacy":
                self.logger.info(
                    "Legacy cookie.pkl token accepted by Windscribe API and "
                    "migrated to cookie.json"
                )
        self._cookie_source = "session"

    def _send(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Send one port API request with the current token in header and form."""
        if method != "GET":
            kwargs["data"] = {
                **kwargs.get("data", {}),
                "session_auth_hash": self.token,
            }
        response = self.client.request(
            method,
            config.EPHEM_URL,
            headers={"authorization": f"Bearer {self.token}"},
            **kwargs,
        )
        return self._data(response)

    def _request(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Retry a rejected cached session once, using a newly obtained token."""
        logged_in = self.token is None
        if logged_in:
            self.logger.info("No usable cached session; logging in to Windscribe API")
            self.login()
        try:
            result = self._send(method, **kwargs)
        except WindscribeError as exc:
            if exc.code not in _SESSION_ERROR_CODES or logged_in:
                raise
            self.logger.warning(
                "Saved API session rejected (code %s); trying one fresh API login",
                exc.code,
            )
            self.token = None
            self._token_validated = False
            self.login()
            result = self._send(method, **kwargs)

        if not self._token_validated:
            self._token_validated = True
            self._persist_token()
        return result

    def login(self) -> None:
        """Exchange a one-time auth token and credentials for an API session."""
        response = self.client.post(
            config.AUTH_TOKEN_URL,
            json={"username": self.username, "password": self.password},
            headers={"authorization": "Bearer anonymous"},
        )
        auth = self._data(response)
        if not all(
            isinstance(auth.get(key), str) and auth[key]
            for key in ("token", "token_sig")
        ):
            raise WindscribeError(None, "login response is missing its one-time token")
        payload: dict[str, Any] = {
            "username": self.username,
            "password": self.password,
            "session_type_id": 1,
            "platform": "legacy-web",
            "secure_token": auth["token"],
            "secure_token_sig": auth["token_sig"],
        }
        if self.totp:
            try:
                payload["2fa_code"] = pyotp.TOTP(self.totp).now()
            except (ValueError, TypeError, binascii.Error) as exc:
                raise ValueError(
                    "WS_TOTP must contain the permanent Base32 secret, "
                    "not a rotating six-digit code"
                ) from exc
        response = self.client.post(
            config.SESSION_URL,
            json=payload,
            headers={"authorization": "Bearer anonymous"},
        )
        token = self._data(response).get("session_auth_hash")
        if not isinstance(token, str) or not token.strip():
            raise WindscribeError(None, "login response is missing its session token")
        self.token = token
        self._token_validated = False
        self._cookie_source = None
        self.logger.info("Windscribe API login successful")

    @staticmethod
    def _port(value: Any) -> int:
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 65535
        ):
            raise WindscribeError(None, "API returned an invalid port")
        return value

    def get_ephm_port(self) -> int | None:
        data = self._request("GET")
        if data.get("success") != 1 or "epf" not in data:
            raise WindscribeError(None, "could not read the current ephemeral port")
        epf = data["epf"]
        if epf is False or epf is None:
            return None
        if not isinstance(epf, dict):
            raise WindscribeError(None, "API returned an invalid ephemeral port record")
        return self._port(epf.get("ext"))

    def delete_ephm_port(self, port: int) -> None:
        result = self._request("DELETE", data={"id": self._port(port)})
        if result.get("success") != 1:
            raise WindscribeError(None, "could not delete the existing ephemeral port")
        self.logger.debug("Ephemeral port deleted: %s", port)

    def set_matching_port(self) -> int:
        result = self._request("POST")
        if result.get("success") != 1 or not isinstance(result.get("epf"), dict):
            raise WindscribeError(None, "could not request a matching ephemeral port")
        external = self._port(result["epf"].get("ext"))
        internal = self._port(result["epf"].get("int"))
        if external != internal:
            raise WindscribeError(None, "Windscribe returned non-matching ports")
        self.logger.info("New matching ephemeral port: %s", internal)
        return internal

    def setup(self) -> int:
        current = self.get_ephm_port()
        if current is not None:
            self.delete_ephm_port(current)
        return self.set_matching_port()

    def close(self) -> None:
        self.client.close()
