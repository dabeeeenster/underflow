# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=12"]
# ///
"""Get or patch a storage-mode Lovelace dashboard over the HA websocket.

  HASS_URL=... HASS_TOKEN=... uv run tools/ha_dashboard.py get <url_path> > cfg.json
  HASS_URL=... HASS_TOKEN=... uv run tools/ha_dashboard.py save <url_path> cfg.json
"""
import asyncio, json, os, sys
import websockets


async def ws_call(msgs):
    url = os.environ["HASS_URL"].rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": os.environ["HASS_TOKEN"]}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth failed")
        out = []
        for i, m in enumerate(msgs, 1):
            await ws.send(json.dumps({"id": i, **m}))
            r = json.loads(await ws.recv())
            if not r.get("success"):
                sys.exit(f"failed: {r}")
            out.append(r.get("result"))
        return out


cmd, url_path = sys.argv[1], sys.argv[2]
if cmd == "get":
    cfg, = asyncio.run(ws_call([{"type": "lovelace/config", "url_path": url_path, "force": True}]))
    print(json.dumps(cfg, indent=1))
elif cmd == "save":
    cfg = json.load(open(sys.argv[3]))
    asyncio.run(ws_call([{"type": "lovelace/config/save", "url_path": url_path, "config": cfg}]))
    print("saved")
