"""Fetches 1m USDT/IRT bars of Bitpin for the last N days.

Bitpin serves TradingView bars without a key from Iran
(api.bitpin.ir/v1/mkt/tv/get_bars). Tabdeal shows no USDT chart
("disabled by order of the central bank"), and api.nobitex.ir does not
connect from the developer machine or the server (2026-10-23).
Usage: python3 -I research/scripts/bitpin_fetch.py OUT_JSON [DAYS] [SYMBOL]
Writes {ts: [open, high, low, close, volume]} in epoch seconds.
"""
import json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

out = sys.argv[1]
days = int(sys.argv[2]) if len(sys.argv) > 2 else 62
symbol = sys.argv[3] if len(sys.argv) > 3 else "USDT_IRT"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def day(start):
    url = (f"https://api.bitpin.ir/v1/mkt/tv/get_bars/?symbol={symbol}&res=1"
           f"&from={start}&to={start + 86400}")
    for k in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with OP.open(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 * (k + 1))
    return []


end = int(time.time()) // 60 * 60
starts = [end - (i + 1) * 86400 for i in range(days)]
rows = {}
with ThreadPoolExecutor(6) as pool:
    for bars in pool.map(day, starts):
        for b in bars:
            rows[int(b["ts"])] = [float(b["open"]), float(b["high"]), float(b["low"]),
                                  float(b["close"]), float(b["volume"])]
json.dump(rows, open(out, "w"))
print(symbol, len(rows), "bars")
