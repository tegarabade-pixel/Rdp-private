"""PrivateRDP standalone signaling bridge.

Connects one public room websocket (role=windows) to the local WebRTC host IPC.
It carries SDP/ICE/control JSON only. Video/audio/input never traverse this bridge.
No UUC token/OAuth/USB protocol is used.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import urllib.parse
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("private-rdp-signal")
SIGNAL_URL = os.environ.get("RDP_SIGNAL_URL", "wss://universal-usb-signal.reiseakari.workers.dev/ws").strip()
ROOM = "".join(ch for ch in os.environ.get("RDP_ROOM", "PRIVATE-RDP-2026").upper() if ch.isalnum())
LOCAL_URL = os.environ.get("RDP_DESKTOP_IPC", "ws://127.0.0.1:8765")
ICE_JSON = os.environ.get("RDP_ICE_SERVERS_JSON", "").strip()

APP_TO_HOST = {
    "rtc-offer", "rtc-ice", "rtc-restart", "rtc-answer-replay",
    "rtc-answer-ack", "rtc-signal-resync", "desktop-control",
}
HOST_TO_APP = {"rtc-answer", "rtc-offer-ack", "rtc-ice", "desktop-state", "desktop-error"}


def public_endpoint() -> str:
    if len(ROOM) < 12:
        raise RuntimeError("RDP_ROOM must normalize to at least 12 alphanumeric characters")
    u = urllib.parse.urlsplit(SIGNAL_URL)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    q.update({"room": ROOM, "role": "windows"})
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/ws", urllib.parse.urlencode(q), u.fragment))


def msg_type(raw: str) -> str:
    try:
        obj = json.loads(raw)
        return str(obj.get("type", "")) if isinstance(obj, dict) else ""
    except Exception:
        return ""


async def pipe_public_to_local(public, local):
    async for raw in public:
        if not isinstance(raw, str):
            continue
        typ = msg_type(raw)
        if typ in APP_TO_HOST:
            await local.send(raw)
        elif typ not in {"hello", "peer-joined", "peer-left", "app-ping", "app-pong"}:
            LOG.debug("ignored public type=%s", typ)


async def pipe_local_to_public(local, public):
    async for raw in local:
        if not isinstance(raw, str):
            continue
        typ = msg_type(raw)
        if typ in HOST_TO_APP:
            await public.send(raw)


def ice_config_payload() -> dict:
    if ICE_JSON:
        try:
            parsed = json.loads(ICE_JSON)
            if not isinstance(parsed, list):
                raise ValueError("RDP_ICE_SERVERS_JSON must be a JSON array")
            return {"type": "desktop-config", "iceServers": parsed, "iceProvider": "runner-config", "iceMode": "auto"}
        except Exception as exc:
            LOG.warning("ignoring invalid RDP_ICE_SERVERS_JSON: %s", exc)
    return {"type": "desktop-config", "iceServers": [], "iceProvider": "private-rdp-stun", "iceMode": "stun-only"}


async def presence_loop(public):
    cfg = ice_config_payload()
    while True:
        await public.send(json.dumps(cfg, separators=(",", ":")))
        await public.send(json.dumps({
            "type": "desktop-state", "state": "host-ready", "desktopProtocol": 3,
            "turnMode": cfg.get("iceMode", "stun-only"),
            "iceProvider": cfg.get("iceProvider", "private-rdp-stun"),
        }, separators=(",", ":")))
        await asyncio.sleep(2.0)


async def run_once():
    endpoint = public_endpoint()
    LOG.info("connecting room=%s signal=%s", ROOM, SIGNAL_URL)
    # Host IPC is local-only. Reconnecting it is cheap; the host preserves an active PC
    # for its signaling grace window when this bridge briefly disappears.
    async with websockets.connect(LOCAL_URL, max_size=4*1024*1024, ping_interval=10, ping_timeout=20) as local:
        async with websockets.connect(endpoint, max_size=4*1024*1024, ping_interval=10, ping_timeout=20) as public:
            LOG.info("SIGNAL READY room=%s", ROOM)
            a = asyncio.create_task(pipe_public_to_local(public, local))
            b = asyncio.create_task(pipe_local_to_public(local, public))
            c = asyncio.create_task(presence_loop(public))
            done, pending = await asyncio.wait({a, b, c}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass


async def main():
    delay = 0.25
    while True:
        try:
            await run_once()
            delay = 0.25
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("signal bridge reconnect: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(delay)
            delay = min(3.0, delay * 1.7)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
