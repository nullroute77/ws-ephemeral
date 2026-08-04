"""
############################
# Author: Dhruvin Shah
# Date: 18th Sep 2023
############################

A worker that keep track of qBitTorrent connection
"""

import logging

import config
from lib.gluetun import GluetunManager
from lib.qbit import QbitManager

# Lets assume we have proper connection with the qBitTorrent
HEARTBEAT = True
_DESIRED_PORT: int | None = None

logger = logging.getLogger("main.monitor")


def set_desired_port(port: int) -> None:
    """Store the Windscribe port that Gluetun should expose."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Desired port must be between 1 and 65535")

    global _DESIRED_PORT
    _DESIRED_PORT = port
    logger.debug("Desired forwarded port recorded: %s", port)


def reconcile_gluetun() -> bool | None:
    """Ensure Gluetun exposes the latest port obtained from Windscribe.

    Returns ``None`` when synchronization is disabled or no Windscribe port has
    been obtained yet, otherwise whether Gluetun is synchronized. Failures are
    logged and retried by the next scheduled monitor run.
    """
    if not config.GLUETUN_URL or _DESIRED_PORT is None:
        return None

    try:
        with GluetunManager(
            url=config.GLUETUN_URL,
            timeout=config.REQUEST_TIMEOUT,
            api_key=config.GLUETUN_API_KEY,
            username=config.GLUETUN_USERNAME,
            password=config.GLUETUN_PASSWORD,
        ) as gluetun:
            forwarded_ports = gluetun.get_forwarded_ports()
            if forwarded_ports == [_DESIRED_PORT]:
                logger.debug(
                    "Gluetun VPN firewall already exposes port %s", _DESIRED_PORT
                )
                return True

            logger.warning(
                "Gluetun VPN firewall ports %s do not match desired port %s; repairing",
                forwarded_ports,
                _DESIRED_PORT,
            )
            return gluetun.set_forwarded_port(_DESIRED_PORT)
    except Exception:
        logger.exception(
            "Unable to reconcile Gluetun VPN firewall; retrying on the next "
            "monitor cycle"
        )
        return False


def monitor() -> bool:
    """Monitor qBittorrent and reconcile Gluetun's VPN firewall port."""
    global HEARTBEAT
    try:
        QbitManager(
            host=config.QBIT_HOST,
            port=config.QBIT_PORT,
            username=config.QBIT_USERNAME,
            password=config.QBIT_PASSWORD,
            api_key=config.QBIT_API_KEY,
        )
    except Exception:
        logger.exception("Something wrong with Qbit, it's not accessible")
        HEARTBEAT = False
    else:
        logger.debug("Heartbeat detected")
        HEARTBEAT = True

    reconcile_gluetun()
    return HEARTBEAT
