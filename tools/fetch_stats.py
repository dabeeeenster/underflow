# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=12"]
# ///
"""Pull long-term statistics out of a Home Assistant recorder to CSV, for offline fitting.

Usage:
  HASS_URL=https://ha.example HASS_TOKEN=... uv run tools/fetch_stats.py \
      --start 2026-03-20 --end 2026-05-01 --period hour --out data/spring.csv \
      sensor.a sensor.b ...

Writes one row per period start with a column per statistic id (mean, or change for
counters when --change is given for that id via 'id:change').
"""
import argparse, asyncio, csv, json, os, sys
import websockets


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", help="statistic ids; append :change to use the change column")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--period", default="hour", choices=["5minute", "hour", "day"])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    url = os.environ["HASS_URL"].rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    token = os.environ["HASS_TOKEN"]
    ids = [(i.split(":")[0], i.split(":")[1] if ":" in i else "mean") for i in a.ids]

    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"auth failed: {auth}")
        await ws.send(json.dumps({"id": 1, "type": "recorder/statistics_during_period",
                                  "start_time": a.start + "T00:00:00+00:00", "end_time": a.end + "T00:00:00+00:00",
                                  "statistic_ids": [i for i, _ in ids], "period": a.period,
                                  "types": ["mean", "change"]}))
        msg = json.loads(await ws.recv())
        if not msg.get("success"):
            sys.exit(f"query failed: {msg}")
        result = msg["result"]

    rows = {}
    for sid, col in ids:
        for r in result.get(sid, []):
            rows.setdefault(r["start"], {})[sid] = r.get(col)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["start_ms"] + [i for i, _ in ids])
        for start in sorted(rows):
            w.writerow([start] + [rows[start].get(i) for i, _ in ids])
    print(f"wrote {len(rows)} rows x {len(ids)} series to {a.out}")
    for sid, _ in ids:
        n = sum(1 for r in rows.values() if r.get(sid) is not None)
        print(f"  {sid}: {n} values")

asyncio.run(main())
