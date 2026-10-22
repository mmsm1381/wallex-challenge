"""Fetch 60 days of Wallex 1m candles for the tradeable pairs, plus a few depth snapshots for spreads."""
import json, time, urllib.request, statistics

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(u):
    for k in range(4):
        try:
            with OP.open(urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 * (k + 1))
    return {}

COINS_USDT = ['BTC', 'ETH', 'XRP', 'SOL', 'PEPE', 'DOGE', 'BNB', 'ADA', 'LTC', 'LINK', 'SUI', 'ARB']
COINS_TMN = ['BTC', 'ETH', 'XRP', 'SOL', 'PEPE', 'DOGE', 'BNB', 'ARB', 'ADA', 'LTC', 'SUI', 'TRX', 'LINK', 'SHIB']
SYMS = ['USDTTMN'] + [c + 'USDT' for c in COINS_USDT] + [c + 'TMN' for c in COINS_TMN]

spreads = {s: [] for s in SYMS}
for rnd in range(3):
    for s in SYMS:
        d = get(f'https://api.wallex.ir/v1/depth?symbol={s}').get('result') or {}
        try:
            b, a = float(d['bid'][0]['price']), float(d['ask'][0]['price'])
            spreads[s].append((a - b) / ((a + b) / 2) * 100)
        except Exception:
            pass
        time.sleep(0.15)
    time.sleep(10)
spread = {s: statistics.median(v) if v else None for s, v in spreads.items()}
print('median spreads %:', {k: round(v, 3) for k, v in spread.items() if v is not None}, flush=True)

end = int(time.time()) // 60 * 60
start = end - 61 * 86400
out = {'spread': spread, 'wallex': {}, 'start': start, 'end': end}
for s in SYMS:
    rows, t = {}, start
    while t < end:
        t2 = min(t + 86400, end)
        d = get(f'https://api.wallex.ir/v1/udf/history?symbol={s}&resolution=1&from={t}&to={t2}')
        if d.get('s') == 'ok':
            for i, ts in enumerate(d['t']):
                rows[int(ts)] = (float(d['c'][i]), float(d['v'][i]))
        t = t2
        time.sleep(0.12)
    out['wallex'][s] = rows
    print(s, len(rows), flush=True)
    json.dump(out, open('wallex60.json', 'w'))
print('done')
