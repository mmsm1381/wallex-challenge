"""Fetch 1-minute candles: Wallex udf history and Binance spot daily klines (data.binance.vision)."""
import csv, io, json, sys, time, urllib.request, zipfile, datetime as dt

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def raw(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    for i in range(4):
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            if i == 3:
                raise
            time.sleep(1.5 * (i + 1))

days = 7
now = int(time.time()) // 60 * 60
start = now - days * 86400
out = {'wallex': {}, 'binance': {}, 'start': start, 'end': now}

WALLEX = ['BTCUSDT', 'BTCTMN', 'USDTTMN', 'ETHTMN', 'ETHUSDT', 'SOLTMN', 'XRPTMN', 'PEPETMN', 'DOGETMN', 'XAUTTMN', 'XAUTUSDT', 'SOLUSDT', 'BNBTMN', 'ARBTMN', 'XRPUSDT', 'PEPEUSDT', 'DOGEUSDT']
for s in WALLEX:
    t, rows = start, {}
    while t < now:
        t2 = min(t + 86400, now)
        d = json.loads(raw(f'https://api.wallex.ir/v1/udf/history?symbol={s}&resolution=1&from={t}&to={t2}'))
        if d.get('s') == 'ok':
            for i, ts in enumerate(d['t']):
                rows[int(ts)] = (float(d['o'][i]), float(d['h'][i]), float(d['l'][i]), float(d['c'][i]), float(d['v'][i]))
        t = t2
        time.sleep(0.2)
    out['wallex'][s] = rows
    print('wallex', s, len(rows), flush=True)
json.dump(out, open('candles.json', 'w'))

BIN = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'PEPEUSDT', 'DOGEUSDT', 'BNBUSDT', 'ARBUSDT', 'PAXGUSDT']
d0 = dt.datetime.utcfromtimestamp(start).date()
d1 = dt.datetime.utcfromtimestamp(now).date()
for s in BIN:
    rows = {}
    day = d0
    while day <= d1:
        url = f'https://data.binance.vision/data/spot/daily/klines/{s}/1m/{s}-1m-{day.isoformat()}.zip'
        try:
            z = zipfile.ZipFile(io.BytesIO(raw(url)))
            for r in csv.reader(io.TextIOWrapper(z.open(z.namelist()[0]))):
                ot = int(r[0])
                ts = ot // 1000000 if ot > 10**14 else ot // 1000
                rows[ts] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        except Exception as e:
            print('  miss', s, day, str(e)[:60])
        day += dt.timedelta(days=1)
    out['binance'][s] = rows
    print('binance', s, len(rows), flush=True)
json.dump(out, open('candles.json', 'w'))
