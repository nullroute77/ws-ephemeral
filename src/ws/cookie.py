"""Load and save Windscribe session cookies."""

import json
import logging
import os
import pickle
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Literal

from httpx import Cookies

import config

logger = logging.getLogger(__name__)

CookieSource = Literal["session", "legacy", "browser-import"]


@dataclass(frozen=True)
class LoadedCookies:
    """Cookies loaded from persistent storage or a browser export."""

    cookies: Cookies
    source: CookieSource


def default_cookie() -> Cookies:
    """Build the default unauthenticated cookie jar."""
    cookie = Cookies()
    cookie.set("i_can_has_cookie", "1")
    cookie.set("ref", "https://windscribe.com/")
    return cookie


def _is_windscribe_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return normalized == "windscribe.com" or normalized.endswith(".windscribe.com")


def _read_json_cookie_entries(path: Path) -> list[dict[str, Any]]:
    """Read either a cookie array or a browser storage-state object."""
    try:
        with open(path, encoding="utf-8") as cookie_file:
            payload = json.load(cookie_file)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Cookie file {path} is not valid JSON") from exc

    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
        entries = payload["cookies"]
    else:
        raise ValueError(
            f"Cookie file {path} must contain a JSON array or a 'cookies' array"
        )

    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError(f"Cookie file {path} contains a non-object cookie entry")

    return entries


def _cookie_from_entry(entry: dict[str, Any], *, browser_import: bool) -> Cookie:
    name = entry.get("name")
    value = entry.get("value")
    domain = entry.get("domain", "")
    path = entry.get("path", "/")

    if not isinstance(name, str) or not name:
        raise ValueError("Each cookie must have a non-empty string 'name'")
    if not isinstance(value, str):
        raise ValueError(f"Cookie {name!r} must have a string 'value'")
    if not isinstance(domain, str):
        raise ValueError(f"Cookie {name!r} must have a string 'domain'")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"Cookie {name!r} must have an absolute 'path'")
    if browser_import and not domain:
        raise ValueError(f"Browser cookie {name!r} is missing its domain")

    expires_value = entry.get("expires", entry.get("expirationDate"))
    expires: int | None = None
    if isinstance(expires_value, (int, float)) and not isinstance(expires_value, bool):
        if expires_value > 0:
            expires = int(expires_value)

    secure = entry.get("secure", False)
    if not isinstance(secure, bool):
        raise ValueError(f"Cookie {name!r} must have a boolean 'secure' value")

    http_only = entry.get("httpOnly", False)
    if not isinstance(http_only, bool):
        raise ValueError(f"Cookie {name!r} must have a boolean 'httpOnly' value")

    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": "true"} if http_only else {},
        rfc2109=False,
    )


def _cookies_from_json(path: Path, *, browser_import: bool) -> Cookies:
    entries = _read_json_cookie_entries(path)
    cookies = Cookies()
    imported = 0

    for entry in entries:
        cookie = _cookie_from_entry(entry, browser_import=browser_import)
        if browser_import and not _is_windscribe_domain(cookie.domain):
            continue
        cookies.jar.set_cookie(cookie)
        imported += 1

    if imported == 0:
        qualifier = "Windscribe " if browser_import else ""
        raise ValueError(f"Cookie file {path} contains no {qualifier}cookies")

    return cookies


def _load_legacy_cookie(path: Path) -> Cookies:
    cookies = Cookies()
    with open(path, "rb") as cookie_file:
        # This compatibility path reads the installation's existing trusted file.
        cookie_dict = pickle.load(cookie_file)  # noqa: S301

    if not isinstance(cookie_dict, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in cookie_dict.items()
    ):
        raise ValueError(f"Legacy cookie file {path} has an unexpected format")

    for name, value in cookie_dict.items():
        cookies.set(name, value)
    return cookies


def load_cookie() -> LoadedCookies | None:
    """Load an existing session, legacy pickle, or browser cookie export.

    The maintained JSON session takes priority. Existing pickle sessions remain
    readable for migration, and the browser export is only used when neither
    persistent session file exists.
    """
    if config.WS_COOKIE_JSON.exists():
        return LoadedCookies(
            _cookies_from_json(config.WS_COOKIE_JSON, browser_import=False),
            "session",
        )

    if config.WS_COOKIE.exists():
        logger.warning(
            "Loading legacy cookie file %s; it will be migrated to JSON after "
            "Windscribe validates it",
            config.WS_COOKIE,
        )
        return LoadedCookies(_load_legacy_cookie(config.WS_COOKIE), "legacy")

    if config.WS_COOKIE_IMPORT_FILE is not None:
        if not config.WS_COOKIE_IMPORT_FILE.exists():
            raise FileNotFoundError(
                f"WS_COOKIE_IMPORT_FILE does not exist: {config.WS_COOKIE_IMPORT_FILE}"
            )
        return LoadedCookies(
            _cookies_from_json(config.WS_COOKIE_IMPORT_FILE, browser_import=True),
            "browser-import",
        )

    return None


def save_cookie(cookie: Cookies) -> None:
    """Persist cookies as JSON using an atomic file replacement."""
    entries: list[dict[str, str | int | bool | None]] = []
    for stored in cookie.jar:
        entries.append(
            {
                "name": stored.name,
                "value": stored.value,
                "domain": stored.domain,
                "path": stored.path,
                "secure": stored.secure,
                "expires": stored.expires,
                "httpOnly": stored.has_nonstandard_attr("HttpOnly"),
            }
        )

    config.WS_COOKIE_JSON.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.WS_COOKIE_JSON.with_name(config.WS_COOKIE_JSON.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as cookie_file:
        json.dump({"version": 1, "cookies": entries}, cookie_file, indent=2)
        cookie_file.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(config.WS_COOKIE_JSON)
