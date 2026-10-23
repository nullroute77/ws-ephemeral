# WS-EPHEMERAL

> [!WARNING]
> Use a single renewal container per Windscribe account and keep the default
> six-day interval. Each container start also rotates the port. Avoid repeated
> restarts or login attempts while troubleshooting authentication failures.

This fork uses Windscribe's API for authentication and ephemeral port management,
adapted from [upstream's October 2026 API migration](https://github.com/dhruvinsh/ws-ephemeral/commit/2f8fd86d8bb6e51dc7d9b53d9432f915b3883e79).
It keeps Gluetun firewall synchronization, the five-minute monitor, qBittorrent
API-key authentication, and JSON session storage.

This project aims to automate setting up ephemeral port on Windscribe VPN
service for the purpose of port forwarding. Once the setup is done it wait
patiently for next seven days. It delete the ephemeral port setting if any and
set the new one. Useful for some torrent application which are running behind
Windscribe VPN and need to open the ports.

## Docker Setup

> [!important]
> NOTE: V1 is deprecated and should not be used.

### Registries

Images are published to GitHub Container Registry under the repository owner:

- `ghcr.io/nullroute77/ws-ephemeral`

### Tags

Images are built only through a manual workflow run. In the repository's
**Actions** tab, select **Build and publish Docker image**, choose **Run
workflow**, and enter the tag to publish. The tag defaults to `latest`.
For the Windscribe API migration, use the separate `api-test` tag while testing:

```text
ghcr.io/nullroute77/ws-ephemeral:api-test
```

Each manual image build runs the regression tests with Python 3.11 before it
publishes. The **Tests** workflow is also available for manual test-only runs.

### Deploy

#### Cli

```bash
docker run \
-e ONESHOT=false \
-e QBIT_HOST=http://192.168.1.10 \
-e QBIT_PASSWORD=password \
-e QBIT_PORT=8080 \
-e QBIT_PRIVATE_TRACKER=true \
-e QBIT_USERNAME=username \
-e REQUEST_TIMEOUT=10 \
-e WS_COOKIE_PATH=/cookie \
-e WS_DEBUG=False \
-e WS_PASSWORD=password \
-e WS_USERNAME=username \
-e WS_TOTP=permanent_totp_secret \
-v /path/to/local/data:/cookie \
ghcr.io/nullroute77/ws-ephemeral:latest
```

#### Docker-compose

Docker compose file is provided for example. Set `WS_EPHEMERAL_IMAGE` to the
GHCR tag to test, make any other adjustments, and run:

```bash
WS_EPHEMERAL_IMAGE=ghcr.io/nullroute77/ws-ephemeral:api-test docker compose up -d
```

### Environment Variables

| Variable              | Comment                                                                          |
| --------------------- | -------------------------------------------------------------------------------- |
| WS_USERNAME           | WS username                                                                      |
| WS_PASSWORD           | WS password                                                                      |
| WS_TOTP               | Permanent Base32 TOTP secret for Windscribe 2FA                                  |
| WS_DEBUG              | Enable Debug logging                                                             |
| WS_COOKIE_PATH        | Directory where the validated `cookie.json` session is stored                    |
| WS_COOKIE_IMPORT_FILE | Optional path to a browser-exported JSON cookie file                             |
| QBIT_USERNAME         | QBIT username                                                                    |
| QBIT_PASSWORD         | QBIT password                                                                    |
| QBIT_API_KEY          | QBIT API key; alternative to username/password and requires qBittorrent v5.2.0+ |
| QBIT_HOST             | QBIT web address like, https://qbit.xyz.com or http://192.168.1.10               |
| QBIT_PORT             | QBIT web port number like, 443 or 8080                                           |
| QBIT_PRIVATE_TRACKER  | get QBIT ready for private tracker by disabling dht, pex and lsd (true or false) |
| GLUETUN_URL           | Gluetun control server URL; setting it enables VPN firewall port synchronization |
| GLUETUN_API_KEY       | API key for the Gluetun control server, if API-key authentication is configured  |
| GLUETUN_USERNAME      | Username for Gluetun control server basic authentication                        |
| GLUETUN_PASSWORD      | Password for Gluetun control server basic authentication                        |
| ONESHOT               | Run and setup the code only one time so that job can be schedule externally      |
| REQUEST_TIMEOUT       | configurable http api timeout for slow network/busy websites                     |

> [!tip]
> NOTE: for usage see [Docker Setup](#docker-setup) v2 setup guide.

### Windscribe browser cookie import

The API client first tries the `ws_session_auth_hash` token from your existing
`cookie.json`, legacy `cookie.pkl`, or browser export. It validates that token
with Windscribe's port API, without loading the website or scraping CSRF fields.
An accepted legacy or imported token is saved as `cookie.json`; the original
input file remains untouched. Keep the same `WS_COOKIE_PATH` volume when updating.

When there is no usable token, the client logs in through
`https://api.windscribe.com/AuthToken/login` and `/Session`. Keep `WS_USERNAME`,
`WS_PASSWORD`, and, when applicable, `WS_TOTP` configured. Your existing Gluetun,
qBittorrent, and scheduling environment variables do not change.

For a browser import, sign in and export the cookies for `windscribe.com` as
JSON. The export must include `ws_session_auth_hash`; other cookie names alone
cannot authenticate the API. An export without that token falls back to API
credential login. The import accepts the common
browser-export array format and storage-state objects containing a `cookies`
array. Each cookie must include `name`, `value`, `domain`, and `path`; fields
such as `secure`, `httpOnly`, `expirationDate`, and `expires` are understood when
present. After API validation the maintained file contains only the session
token, because unrelated browser cookies are no longer needed.

Place the export in the directory mounted at `WS_COOKIE_PATH` and configure its
container path:

```env
WS_COOKIE_PATH=/cookie
WS_COOKIE_IMPORT_FILE=/cookie/windscribe-export.json
```

For example, the file can look like:

```json
[
  {
    "name": "cookie-name-from-browser",
    "value": "cookie-value-from-browser",
    "domain": ".windscribe.com",
    "path": "/",
    "secure": true,
    "httpOnly": true,
    "expirationDate": 1893456000
  }
]
```

Do not replace the example names and values manually; export all cookies for
the Windscribe domain so the complete authenticated session is available. The
importer ignores cookies for other domains.

The browser export is considered a one-time input. On startup, ws-ephemeral
uses files in this order:

1. `/cookie/cookie.json`, the maintained and validated session.
2. `/cookie/cookie.pkl`, an existing legacy session.
3. `WS_COOKIE_IMPORT_FILE`, the browser export.

If Windscribe accepts the imported session, ws-ephemeral writes
`/cookie/cookie.json` with owner-only permissions. The original export is then
ignored and can be removed along with `WS_COOKIE_IMPORT_FILE`. If a legacy
`cookie.pkl` is present, it must be moved aside to force a browser import. A
valid legacy session is automatically migrated to JSON without deleting the
original file.

Imported and saved tokens are validated against Windscribe's API before they are
persisted. If a cached token is rejected with a session error, ws-ephemeral makes
one fresh username/password/TOTP login attempt and retries that request once.
Other API errors fail the run without repeated login attempts. An API login
that is rejected, or a new token that fails validation, does not overwrite the
existing session file. Unreadable saved caches are ignored for fresh API login.
`WS_TOTP`, when used, must contain the
permanent Base32 setup secret rather than the rotating six-digit code.

Cookie files grant access to the Windscribe account. Keep the cookie directory
private and do not commit exports to Git. Legacy pickle migration accepts only
the old dictionary format and blocks pickle globals; JSON remains the maintained
storage format.

### qBittorrent authentication

Use either a qBittorrent username and password:

```env
QBIT_USERNAME=admin
QBIT_PASSWORD=replace-with-password
```

Or, with qBittorrent v5.2.0 or newer, generate an API key in its WebUI settings
and configure:

```env
QBIT_API_KEY=qbt_replace_with_api_key
```

Do not configure `QBIT_API_KEY` together with `QBIT_USERNAME` or
`QBIT_PASSWORD`. API-key support requires `qbittorrent-api` v2026.5.3 or newer,
which is included in this project.

### Gluetun VPN firewall synchronization

ws-ephemeral can replace Gluetun's runtime VPN firewall port whenever
Windscribe assigns a new matching port. This removes the need to update
`FIREWALL_VPN_INPUT_PORTS` and restart Gluetun for each renewal.
`FIREWALL_INPUT_PORTS` controls Gluetun's default interface and does not need
the Windscribe-assigned port unless you separately want that port reachable
through the default interface.

This integration requires a Gluetun release containing
[`PUT /v1/portforward`](https://github.com/passteque/gluetun/pull/2392).
Gluetun v3.41.2 does not contain that endpoint. Leave Gluetun's
`VPN_PORT_FORWARDING` option off because ws-ephemeral obtains the port from
Windscribe.

Set `GLUETUN_URL` to enable the integration:

```env
GLUETUN_URL=http://gluetun:8000
GLUETUN_API_KEY=replace-with-api-key
```

Use `http://127.0.0.1:8000` instead when ws-ephemeral shares Gluetun's network
namespace. The control server does not need to be published outside the Docker
network.

Gluetun must authorize both the update and verification routes. For API-key
authentication, its `/gluetun/auth/config.toml` can include:

```toml
[[roles]]
name = "ws-ephemeral"
routes = ["PUT /v1/portforward", "GET /v1/portforward"]
auth = "apikey"
apikey = "replace-with-api-key"
```

Alternatively, set `GLUETUN_USERNAME` and `GLUETUN_PASSWORD` for a basic-auth
role. Do not set them together with `GLUETUN_API_KEY`. If the matching Gluetun
role uses `auth = "none"`, only `GLUETUN_URL` is needed.

The change is runtime state, so restarting Gluetun clears it. With
`ONESHOT=false`, ws-ephemeral remembers the latest Windscribe port and checks
Gluetun every five minutes. If Gluetun reports a different port or no port,
ws-ephemeral restores the desired port with `PUT /v1/portforward` and verifies
the result. If Gluetun is still starting, the monitor logs the failure and
retries on its next cycle. With `ONESHOT=true`, run ws-ephemeral again after a
Gluetun restart because no background monitor remains active.

## Unraid Setup

Unraid template is now available under community application.

## Changelog

Located [here](./CHANGELOG.md)

## Privacy

I assure you that nothing is being collected or logged. Your credentials are
safe and set via environment variable only. Still If you have further questions
or concerns, please open an issue here.

## Roadmap

- [x] Support 2FA, #19
- [ ] Daemon mode and job mode
  - [ ] Rest API (useful for cron/script job)
  - [ ] Separate port renewal, qbittorrent update and private tracker logic
  - [ ] Random job time for cron job #15
- [ ] Allow to run custom script (for now Bash script only) #12
- [ ] Support for deluge
- [x] Gluetun support [#2392](https://github.com/passteque/gluetun/pull/2392)

## License

[GPL3](LICENSE.md)
