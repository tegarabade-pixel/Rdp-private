from __future__ import annotations
import asyncio, json, os, sys
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

INPUT = os.environ.get("RDP_TURN_PROBE_INPUT", "")
OUTPUT = os.environ.get("RDP_TURN_PROBE_OUTPUT", "")


def urls_of(item):
    if not isinstance(item, dict): return []
    urls = item.get("urls", item.get("url", []))
    if isinstance(urls, str): urls = [urls]
    return [str(x).strip() for x in (urls or []) if str(x).strip()]


def is_turn(url):
    return url.lower().startswith(("turn:", "turns:"))


def rank(url):
    u = url.lower()
    if u.startswith("turn:") and "transport=udp" in u: return 0
    if u.startswith("turn:") and "transport=" not in u: return 1
    if u.startswith("turns:") and ":443" in u: return 2
    if u.startswith("turn:") and ":443" in u and "transport=tcp" in u: return 3
    if u.startswith("turn:") and ":80" in u and "transport=tcp" in u: return 4
    if "transport=tcp" in u: return 5
    return 9


def candidate_types(sdp):
    out=[]
    for line in (sdp or "").splitlines():
        if "candidate:" not in line or " typ " not in line: continue
        p=line.strip().split()
        try: out.append(p[p.index("typ")+1].lower())
        except Exception: pass
    return sorted(set(out))


async def gather(url, username, credential, timeout=16):
    srv=RTCIceServer(urls=[url], username=username, credential=credential)
    pc=RTCPeerConnection(RTCConfiguration(iceServers=[srv]))
    pc.createDataChannel("privaterdp-turn-probe")
    try:
        offer=await pc.createOffer()
        await asyncio.wait_for(pc.setLocalDescription(offer), timeout=timeout)
        types=candidate_types(pc.localDescription.sdp if pc.localDescription else "")
        return "relay" in types, types, ""
    except Exception as e:
        return False, [], f"{type(e).__name__}: {str(e)[:180]}"
    finally:
        await pc.close()


async def main():
    if not INPUT or not OUTPUT:
        raise SystemExit("RDP_TURN_PROBE_INPUT/OUTPUT missing")
    items=json.loads(open(INPUT,encoding="utf-8").read())
    endpoints=[]
    for item in items if isinstance(items,list) else []:
        if not isinstance(item,dict): continue
        for u in urls_of(item):
            if is_turn(u):
                endpoints.append({"url":u,"username":str(item.get("username","")),"credential":str(item.get("credential",""))})
    endpoints.sort(key=lambda e: rank(e["url"]))
    if not endpoints:
        result={"ready":False,"reason":"no-turn-endpoint","verifiedIceServers":[],"errors":[]}
        open(OUTPUT,"w",encoding="utf-8").write(json.dumps(result))
        print("TURN_VERIFY=NO_TURN_ENDPOINT")
        return
    verified=[]; errors=[]
    for ep in endpoints[:8]:
        safe=ep["url"].split("?",1)[0]
        print(f"TURN_VERIFY trying {safe}", flush=True)
        ok,types,err=await gather(ep["url"], ep["username"], ep["credential"])
        if ok:
            item={"urls":[ep["url"]]}
            if ep["username"]: item["username"]=ep["username"]
            if ep["credential"]: item["credential"]=ep["credential"]
            verified.append(item)
            print(f"TURN_VERIFY PASS {safe} types={types}", flush=True)
        else:
            errors.append({"endpoint":safe,"types":types,"error":err})
            print(f"TURN_VERIFY FAIL {safe} types={types} error={err}", flush=True)
    result={"ready":bool(verified),"reason":"relay-ok" if verified else "no-relay-candidate","verifiedIceServers":verified,"errors":errors}
    open(OUTPUT,"w",encoding="utf-8").write(json.dumps(result))
    if not verified: raise SystemExit(3)

asyncio.run(main())
