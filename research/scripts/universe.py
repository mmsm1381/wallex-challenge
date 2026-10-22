"""Tradeable Wallex universe for the challenge account: all spot markets minus the organizers' blocked list."""
import json, re, sys
blocked_txt = open(sys.argv[1], encoding='utf-8').read()
blocked = {m.replace('/', '') for m in re.findall(r'\b([0-9A-Z]+/(?:TMN|USDT))\b', blocked_txt)}
syms = json.load(open('markets.json'))['result']['symbols']
hector = {m['symbol']: m for m in json.load(open('hector_markets.json'))['result']['markets']}
def f(x):
    try: return float(x)
    except: return 0.0
usdt = (f(syms['USDTTMN']['stats']['bidPrice']) + f(syms['USDTTMN']['stats']['askPrice'])) / 2
rows = []
for k, s in syms.items():
    if k in blocked or not hector.get(k, {}).get('is_spot', True):
        continue
    st = s['stats']; bid, ask = f(st.get('bidPrice')), f(st.get('askPrice'))
    mid = (bid + ask) / 2
    vol_tmn = f(st.get('24h_quoteVolume')) * (1 if s['quoteAsset'] == 'TMN' else usdt)
    rows.append((k, s['quoteAsset'], (ask - bid) / mid * 100 if mid else None, vol_tmn / 1e9, s.get('minNotional')))
print('blocked parsed:', len(blocked), '| tradeable spot markets:', len(rows), '| unknown blocked symbols:', sorted(b for b in blocked if b not in syms)[:10])
for q in ('TMN', 'USDT'):
    r = sorted([x for x in rows if x[1] == q], key=lambda x: -x[3])
    print(f'\nTop tradeable {q} markets by 24h volume (B toman), spread % from the earlier snapshot:')
    print('  ' + ', '.join(f"{x[0]} {x[3]:.1f}B/{x[2]:.2f}%" for x in r[:30] if x[2] is not None))
json.dump([x[0] for x in rows], open('tradeable.json', 'w'))
