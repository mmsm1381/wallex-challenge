"""Snapshot Wallex order books, compare with OKX, and check triangle gaps."""
import json, time, urllib.request

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def get(url, hdrs=False):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with OPENER.open(req, timeout=20) as r:
        body = json.loads(r.read())
        return (body, dict(r.headers)) if hdrs else body

SYMS = ['USDTTMN', 'BTCTMN', 'ETHTMN', 'XAUTTMN', 'BTCUSDT', 'ETHUSDT', 'XAUTUSDT',
        'PEPETMN', 'PUMPTMN', 'SOLTMN', 'XRPTMN', 'DOGETMN', 'ARBTMN', 'BNBTMN',
        'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'PEPEUSDT']
books = {}
for s in SYMS:
    b, h = get(f'https://api.wallex.ir/v1/depth?symbol={s}', hdrs=True)
    books[s] = b['result']
    rl = {k: v for k, v in h.items() if 'ratelimit' in k.lower()}
    time.sleep(0.3)
print('rate-limit headers (last):', rl)

usdt_mid = None
print(f"\n{'sym':10s} {'bid':>14s} {'ask':>14s} {'spr%':>7s} {'depth±0.1%':>12s} {'±0.5%':>12s} {'±1%':>12s} (toman-equiv, both sides)")
mids = {}
for s in SYMS:
    r = books[s]
    bids = [(float(x['price']), float(x['quantity'])) for x in r['bid']]
    asks = [(float(x['price']), float(x['quantity'])) for x in r['ask']]
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2
    mids[s] = (bb, ba, mid)
    if s == 'USDTTMN':
        usdt_mid = mid
    conv = 1.0 if s.endswith('TMN') else usdt_mid
    def depth(pct):
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        return sum(p * q for p, q in bids if p >= lo) * conv + sum(p * q for p, q in asks if p <= hi) * conv
    print(f"{s:10s} {bb:14.6g} {ba:14.6g} {(ba-bb)/mid*100:7.3f} {depth(0.1)/1e6:11.1f}M {depth(0.5)/1e6:11.1f}M {depth(1)/1e6:11.1f}M  levels={len(bids)}/{len(asks)}")

print('\nTriangle check (gross, before fees): TMN -> X (buy XTMN at ask) -> USDT (sell XUSDT at bid) -> TMN (sell USDTTMN at bid)')
ub, ua, um = mids['USDTTMN']
for x in ['BTC', 'ETH', 'XAUT', 'SOL', 'XRP', 'DOGE', 'PEPE']:
    if f'{x}TMN' not in mids or f'{x}USDT' not in mids:
        continue
    tb, ta, tm = mids[f'{x}TMN']
    sb, sa, sm = mids[f'{x}USDT']
    fwd = (1 / ta) * sb * ub - 1          # TMN->X->USDT->TMN
    rev = (1 / ua) / sa * tb - 1          # TMN->USDT->X->TMN
    implied = sm * um
    print(f"  {x:5s} TMN mid={tm:.6g} implied(USDTmkt*USDTTMN)={implied:.6g} dev={(tm/implied-1)*100:+.3f}%  fwd={fwd*100:+.3f}% rev={rev*100:+.3f}%")

print('\nWallex USDT markets vs OKX spot (mid):')
okx = {}
for x in ['BTC', 'ETH', 'XAUT', 'SOL', 'XRP', 'DOGE', 'PEPE']:
    try:
        t = get(f'https://www.okx.com/api/v5/market/ticker?instId={x}-USDT')['data'][0]
        okx[x] = (float(t['bidPx']) + float(t['askPx'])) / 2
    except Exception as e:
        print('okx fail', x, e)
for x, om in okx.items():
    if f'{x}USDT' in mids:
        wm = mids[f'{x}USDT'][2]
        print(f"  {x:5s} wallex={wm:.6g} okx={om:.6g} diff={(wm/om-1)*100:+.3f}%")
    if f'{x}TMN' in mids:
        wm = mids[f'{x}TMN'][2]
        print(f"  {x:5s} TMN-implied USDT price={wm/um:.6g} vs okx diff={(wm/um/om-1)*100:+.3f}%")
print('\nUSDTTMN mid:', um, ' timestamp:', time.strftime('%Y-%m-%d %H:%M:%S'))
json.dump({'books': books, 'okx': okx, 'ts': time.time()}, open('snap.json', 'w'))
