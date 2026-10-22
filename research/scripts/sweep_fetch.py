"""Fetches 61 days of 1m OHLC: Wallex (udf API) and Binance (data.binance.vision).

Usage (from the repo root): python3 -I research/scripts/sweep_fetch.py OUT_DIR
Writes OUT_DIR/wallex_ohlc.json and OUT_DIR/binance/<SYMBOL>.json.
"""
import io, json, os, sys, time, urllib.request, zipfile, csv, datetime

out = sys.argv[1]
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(u, raw=False):
    for k in range(4):
        try:
            with OP.open(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as r:
                data = r.read()
                return data if raw else json.loads(data)
        except Exception as e:
            if raw and getattr(e, "code", None) == 404:
                return None
            time.sleep(2 * (k + 1))
    return None

COINS = ["BTC", "ETH", "XRP", "SOL", "PEPE", "DOGE", "BNB", "ADA", "SHIB", "TRX"]
MARKETS = (["USDTTMN"] + [c + "USDT" for c in COINS[:8]] + [c + "TMN" for c in COINS])
end = int(time.time()) // 60 * 60
start = end - 61 * 86400
from concurrent.futures import ThreadPoolExecutor
def fetch_market(m):
    rows, t = {}, start
    while t < end:
        t2 = min(t + 86400, end)
        d = get(f"https://api.wallex.ir/v1/udf/history?symbol={m}&resolution=1&from={t}&to={t2}") or {}
        if d.get("s") == "ok":
            for i, ts in enumerate(d["t"]):
                rows[int(ts)] = [float(d[k][i]) for k in ("o", "h", "l", "c", "v")]
        t = t2
    print(m, len(rows), flush=True)
    return m, rows
with ThreadPoolExecutor(8) as pool:
    w = dict(pool.map(fetch_market, MARKETS))
json.dump({"start": start, "end": end, "ohlc": w}, open(f"{out}/wallex_ohlc.json", "w"))

os.makedirs(f"{out}/binance", exist_ok=True)
day0 = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).date()
day1 = datetime.datetime.fromtimestamp(end, datetime.timezone.utc).date()
def fetch_binance(c):
    sym = c + "USDT"
    rows = {}
    d = day0
    while d <= day1:
        url = f"https://data.binance.vision/data/spot/daily/klines/{sym}/1m/{sym}-1m-{d:%Y-%m-%d}.zip"
        z = get(url, raw=True)
        if z:
            with zipfile.ZipFile(io.BytesIO(z)) as zf:
                for row in csv.reader(io.TextIOWrapper(zf.open(zf.namelist()[0]))):
                    t = int(row[0])
                    t = t // 1000000 if t > 1e14 else t // 1000  # us or ms
                    rows[t] = [float(row[i]) for i in (1, 2, 3, 4)]
        d += datetime.timedelta(days=1)
    json.dump(rows, open(f"{out}/binance/{sym}.json", "w"))
    print(sym, len(rows), flush=True)
with ThreadPoolExecutor(5) as pool:
    list(pool.map(fetch_binance, COINS))
print("done")
