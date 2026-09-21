"""PrivateRDP v0.2.0 RELAY-LOCK signaling bridge.

Carries SDP/ICE/control JSON only. Media/input remain WebRTC.
The bridge advertises only a Runner-verified TURN configuration and keeps a
single active negotiation epoch so stale answers/candidates are dropped before
reaching the Android client.
"""
from __future__ import annotations
import asyncio, json, logging, os, urllib.parse
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG=logging.getLogger("private-rdp-signal")
SIGNAL_URL=os.environ.get("RDP_SIGNAL_URL","wss://universal-usb-signal.reiseakari.workers.dev/ws").strip()
ROOM="".join(ch for ch in os.environ.get("RDP_ROOM","PRIVATE-RDP-2026").upper() if ch.isalnum())
LOCAL_URL=os.environ.get("RDP_DESKTOP_IPC","ws://127.0.0.1:8765")
ICE_JSON=os.environ.get("RDP_ICE_SERVERS_JSON","").strip()
ICE_PROVIDER=os.environ.get("RDP_ICE_PROVIDER","none").strip() or "none"
ICE_MODE=os.environ.get("RDP_ICE_MODE","relay-only").strip() or "relay-only"
TURN_READY=os.environ.get("RDP_TURN_RELAY_READY","") == "1"
TURN_PROBE=os.environ.get("RDP_TURN_PROBE_RESULT","unknown")

APP_TO_HOST={"rtc-offer","rtc-ice","rtc-restart","rtc-answer-replay","rtc-answer-ack","rtc-signal-resync","desktop-control"}
HOST_TO_APP={"rtc-answer","rtc-offer-ack","rtc-ice","desktop-state","desktop-error"}


def public_endpoint():
    if len(ROOM)<12: raise RuntimeError("RDP_ROOM must normalize to at least 12 alphanumeric characters")
    u=urllib.parse.urlsplit(SIGNAL_URL); q=dict(urllib.parse.parse_qsl(u.query,keep_blank_values=True)); q.update({"room":ROOM,"role":"windows"})
    return urllib.parse.urlunsplit((u.scheme,u.netloc,u.path or "/ws",urllib.parse.urlencode(q),u.fragment))


def decode(raw):
    try:
        o=json.loads(raw)
        return o if isinstance(o,dict) else {}
    except Exception:
        return {}


def has_turn(items):
    for item in items if isinstance(items,list) else []:
        if not isinstance(item,dict): continue
        urls=item.get("urls",item.get("url",[])); urls=[urls] if isinstance(urls,str) else (urls or [])
        for u in urls:
            if str(u).lower().startswith(("turn:","turns:")): return True
    return False


def ice_config_payload():
    if not TURN_READY: raise RuntimeError(f"TURN relay is not verified ({TURN_PROBE})")
    try: parsed=json.loads(ICE_JSON)
    except Exception as exc: raise RuntimeError(f"invalid RDP_ICE_SERVERS_JSON: {exc}") from exc
    if not isinstance(parsed,list) or not has_turn(parsed): raise RuntimeError("relay-only requires at least one TURN URL")
    return {"type":"desktop-config","iceServers":parsed,"iceProvider":ICE_PROVIDER,"iceMode":"relay-only","turnRelayReady":True,"turnProbe":TURN_PROBE}


def tuple_of(obj):
    try: return (int(obj.get("sessionId",0) or 0), int(obj.get("negotiationId",0) or 0), int(obj.get("revision",1) or 1))
    except Exception: return (0,0,0)


def same_epoch(obj, gate):
    sid,nid,rev=tuple_of(obj)
    if sid<=0: return True
    return (sid,nid,rev)==gate["epoch"]


async def pipe_public_to_local(public, local, gate):
    async for raw in public:
        if not isinstance(raw,str): continue
        obj=decode(raw); typ=str(obj.get("type",""))
        if typ=="rtc-offer":
            incoming=tuple_of(obj)
            if incoming[0] > 0:
                if gate["epoch"][0] and incoming < gate["epoch"]:
                    LOG.info("drop stale app offer incoming=%s active=%s",incoming,gate["epoch"]); continue
                gate["epoch"]=incoming
                LOG.info("active signaling epoch=%s",incoming)
            await local.send(raw); continue
        if typ in APP_TO_HOST:
            if typ.startswith("rtc-") and not same_epoch(obj,gate):
                LOG.info("drop stale app %s incoming=%s active=%s",typ,tuple_of(obj),gate["epoch"]); continue
            await local.send(raw)
        elif typ not in {"hello","peer-joined","peer-left","app-ping","app-pong"}:
            LOG.debug("ignored public type=%s",typ)


async def pipe_local_to_public(local, public, gate):
    async for raw in local:
        if not isinstance(raw,str): continue
        obj=decode(raw); typ=str(obj.get("type",""))
        if typ not in HOST_TO_APP: continue
        if typ.startswith("rtc-") and not same_epoch(obj,gate):
            LOG.info("drop stale host %s incoming=%s active=%s",typ,tuple_of(obj),gate["epoch"]); continue
        await public.send(raw)


async def presence_loop(public):
    cfg=ice_config_payload()
    while True:
        await public.send(json.dumps(cfg,separators=(",",":")))
        await public.send(json.dumps({"type":"desktop-state","state":"host-ready","desktopProtocol":4,"turnMode":"relay-only","iceProvider":ICE_PROVIDER,"turnRelayReady":True,"turnProbe":TURN_PROBE,"relayTransport":"udp-first"},separators=(",",":")))
        await asyncio.sleep(2.0)


async def run_once():
    endpoint=public_endpoint(); gate={"epoch":(0,0,0)}
    LOG.info("connecting room=%s provider=%s mode=relay-only",ROOM,ICE_PROVIDER)
    cfg=ice_config_payload()
    LOG.info("TURN config ready endpoints=%d",len(cfg.get("iceServers",[])))
    async with websockets.connect(LOCAL_URL,max_size=4*1024*1024,ping_interval=10,ping_timeout=20) as local:
        async with websockets.connect(endpoint,max_size=4*1024*1024,ping_interval=10,ping_timeout=20) as public:
            LOG.info("SIGNAL READY room=%s",ROOM)
            tasks={asyncio.create_task(pipe_public_to_local(public,local,gate)),asyncio.create_task(pipe_local_to_public(local,public,gate)),asyncio.create_task(presence_loop(public))}
            done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for t in pending: t.cancel()
            for t in done:
                try: t.result()
                except asyncio.CancelledError: pass


async def main():
    delay=.25
    while True:
        try: await run_once(); delay=.25
        except asyncio.CancelledError: raise
        except Exception as exc:
            LOG.warning("signal bridge reconnect: %s: %s",type(exc).__name__,exc)
            await asyncio.sleep(delay); delay=min(3.0,delay*1.7)

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
