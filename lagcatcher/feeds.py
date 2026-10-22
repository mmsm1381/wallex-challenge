"""Websocket feeds: Wallex order books and trades, and global best quotes.

Each `run_*` coroutine keeps one feed alive until the `stop` event is set.
It reconnects with a growing pause after an error or a silent connection,
and it counts messages in a `stats` dict for the status line. The parsers
are pure functions, so the tests can check them with real frames.

Facts about the endpoints, found on 2026-10-21:

* Wallex (Centrifugo v2) answers HTTP 503 unless the client disables
  compression and sends a browser User-Agent. It also answers 503 for
  minutes at a time, while the deprecated version 1 endpoint still works. A depth push is a full top-50
  snapshot of one side under `data.message`. The server sends `{}` as a
  ping and closes the connection if the reply does not come in 8 s.
* Binance is blocked from Iran, and OKX answers HTTP 403 (Cloudflare) to
  the Iranian test host. From the server outside Iran, the Binance
  websocket works on port 443, but port 9443 times out (2026-10-22). Bybit, KuCoin, and Gate websockets connect. Every connection passes `proxy=None`, because the host has an
  `ALL_PROXY` setting that points to a dead proxy.
"""

import asyncio
import json
import logging
import time
import urllib.request
import uuid
from collections.abc import Callable

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

# Version 2 (Centrifugo) is the current API. Version 1 is deprecated, but it
# still works when version 2 answers HTTP 503. The feed tries the other one
# when a connection gives no data.
WALLEX_ENDPOINTS = (("v2", "wss://api.wallex.ir/connection/websocket"),
                    ("v1", "wss://api.wallex.ir/ws"))
WALLEX_DEPTH = "https://api.wallex.ir/v1/depth?symbol={}"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Version 1 accepts 50 subscribe commands per connection and version 2
# accepts 64 channels. 16 markets need 48 channels, which fits both.
WALLEX_MARKETS_PER_CONNECTION = 16
# A connection without any message for this long is dead.
SILENCE_S = 60.0
_NETWORK_ERRORS = (OSError, WebSocketException, asyncio.TimeoutError,
                   json.JSONDecodeError)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

QuoteCallback = Callable[[str, str, float, float, float], None]


def describe_error(error: BaseException) -> str:
    """Returns a short error text. A rejected handshake shows its HTTP status."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"{type(error).__name__} HTTP {status}"
    return f"{type(error).__name__}: {str(error)[:200]}"


def new_stats() -> dict:
    """Returns an empty stats dict for one feed."""
    return {"msgs": 0, "last": 0.0, "connects": 0, "errors": 0, "endpoint": None}


def _http_json(url: str, data: bytes | None = None) -> dict:
    """Runs one HTTP request without a proxy and returns the JSON body."""
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": BROWSER_UA},
        method="POST" if data is not None else "GET")
    with _OPENER.open(request, timeout=15) as response:
        return json.loads(response.read())


def _levels(items: list, descending: bool) -> list[tuple[float, float]]:
    """Converts Wallex level dicts to sorted (price, quantity) tuples."""
    levels = [(float(item["price"]), float(item["quantity"])) for item in items]
    levels.sort(key=lambda level: level[0], reverse=descending)
    return levels


def parse_wallex(line: str) -> tuple | None:
    """Parses one Wallex websocket message.

    Returns:
        ("ping",) for a server ping, ("book", market, "bid" | "ask", levels),
        ("trades", market, [(price, qty, buyer_maker, timestamp), ...]), or
        None for other messages. `buyer_maker` is the Wallex `isBuyOrder`
        field: True means a resting buy order was the maker.
    """
    if line.strip() == "{}":
        return ("ping",)
    try:
        push = json.loads(line).get("push")
        if not push:
            return None
        channel = push.get("channel", "")
        data = (push.get("pub") or {}).get("data")
        if isinstance(data, dict) and "message" in data:
            data = data["message"]
        if channel.startswith("orderbook:"):
            market, _, kind = channel[len("orderbook:"):].partition("@")
            side = "bid" if kind == "buyDepth" else "ask"
            return ("book", market, side, _levels(data or [], side == "bid"))
        if channel.startswith("public:") and channel.endswith("@trade"):
            market = channel[len("public:"):-len("@trade")]
            items = data if isinstance(data, list) else [data]
            trades = [(float(t["price"]), float(t["quantity"]),
                       bool(t["isBuyOrder"]), t.get("timestamp"))
                      for t in items if t]
            return ("trades", market, trades)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return None


def parse_wallex_v1(text: str) -> tuple | None:
    """Parses one message of the deprecated Wallex version 1 websocket.

    Channel data comes as a [channel, body] array, such as
    ["BTCTMN@buyDepth", [levels]] or ["BTCTMN@trade", {trade}]. Other
    messages are objects, such as the connection info, and give None.

    Returns:
        The same tuples as `parse_wallex`, or None.
    """
    try:
        message = json.loads(text)
        if not isinstance(message, list) or len(message) != 2:
            return None
        channel, data = message
        market, _, kind = channel.partition("@")
        if kind in ("buyDepth", "sellDepth"):
            side = "bid" if kind == "buyDepth" else "ask"
            return ("book", market, side, _levels(data or [], side == "bid"))
        if kind == "trade":
            items = data if isinstance(data, list) else [data]
            trades = [(float(t["price"]), float(t["quantity"]),
                       bool(t["isBuyOrder"]), t.get("timestamp"))
                      for t in items if t]
            return ("trades", market, trades)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return None


def parse_bybit(text: str) -> tuple[str, float, float] | None:
    """Parses a Bybit spot orderbook.1 message to (coin, bid, ask)."""
    try:
        message = json.loads(text)
        if not message.get("topic", "").startswith("orderbook.1."):
            return None
        data = message["data"]
        if not data.get("b") or not data.get("a"):
            return None
        return (data["s"][:-len("USDT")], float(data["b"][0][0]),
                float(data["a"][0][0]))
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def parse_okx(text: str) -> tuple[str, float, float] | None:
    """Parses an OKX bbo-tbt message to (coin, bid, ask)."""
    if text == "pong":
        return None
    try:
        message = json.loads(text)
        arg = message.get("arg", {})
        if arg.get("channel") != "bbo-tbt" or not message.get("data"):
            return None
        best = message["data"][0]
        return (arg["instId"].split("-")[0], float(best["bids"][0][0]),
                float(best["asks"][0][0]))
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def parse_kucoin(text: str) -> tuple[str, float, float] | None:
    """Parses a KuCoin /market/ticker message to (coin, bid, ask)."""
    try:
        message = json.loads(text)
        topic = message.get("topic", "")
        if message.get("type") != "message" or not topic.startswith("/market/ticker:"):
            return None
        data = message["data"]
        return (topic.split(":")[1].split("-")[0], float(data["bestBid"]),
                float(data["bestAsk"]))
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def parse_gate(text: str) -> tuple[str, float, float] | None:
    """Parses a Gate spot.book_ticker update to (coin, bid, ask)."""
    try:
        message = json.loads(text)
        if message.get("channel") != "spot.book_ticker" or message.get("event") != "update":
            return None
        result = message["result"]
        return (result["s"].split("_")[0], float(result["b"]), float(result["a"]))
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def parse_binance(text: str) -> tuple[str, float, float] | None:
    """Parses a Binance combined-stream bookTicker push to (coin, bid, ask)."""
    try:
        message = json.loads(text)
        if not message.get("stream", "").endswith("@bookTicker"):
            return None
        data = message["data"]
        return (data["s"][:-len("USDT")], float(data["b"]), float(data["a"]))
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return None


def wallex_rest_book(market: str) -> tuple[list, list]:
    """Returns (bids, asks) of one market from the Wallex REST depth API."""
    result = _http_json(WALLEX_DEPTH.format(market))["result"]
    return _levels(result["bid"], True), _levels(result["ask"], False)


async def _seed_books(markets: list[str], on_book: Callable, since: float) -> None:
    """Loads REST books after a connect, as a subscribe sends no snapshot.

    The engine ignores a seed side when a websocket push for that side came
    after `since`, so an old snapshot cannot replace a newer book.
    """
    for market in markets:
        try:
            bids, asks = await asyncio.to_thread(wallex_rest_book, market)
        except (OSError, ValueError, KeyError) as error:
            log.warning("wallex REST depth %s failed: %s", market, error)
            continue
        on_book(market, "bid", bids, time.time(), since)
        on_book(market, "ask", asks, time.time(), since)
        await asyncio.sleep(0.5)


async def _wallex_subscribe(ws, version: str, markets: list[str]) -> None:
    """Sends the connect and subscribe commands of one endpoint version."""
    if version == "v2":
        await ws.send(json.dumps({"id": 1, "connect": {}}))
    number = 1
    for market in markets:
        for kind in ("buyDepth", "sellDepth", "trade"):
            number += 1
            if version == "v1":
                await ws.send(json.dumps(
                    ["subscribe", {"channel": f"{market}@{kind}"}]))
                continue
            namespace = "public" if kind == "trade" else "orderbook"
            await ws.send(json.dumps({"id": number, "subscribe": {
                "channel": f"{namespace}:{market}@{kind}"}}))


async def _wallex_connection(markets: list[str], on_book: Callable,
                             on_trades: Callable, stats: dict,
                             stop: asyncio.Event) -> None:
    """Keeps one Wallex connection for `markets` alive until `stop`.

    A connection that gives no data makes the next attempt use the other
    endpoint version. A connection that gave data and then closed (version
    1 closes each connection after 30 minutes) reconnects to the same one.
    """
    endpoint = 0
    pause = 1.0
    while not stop.is_set():
        version, url = WALLEX_ENDPOINTS[endpoint]
        msgs_before = stats["msgs"]
        try:
            async with connect(url, proxy=None, compression=None,
                               user_agent_header=BROWSER_UA, open_timeout=15,
                               max_size=2**23) as ws:
                await _wallex_subscribe(ws, version, markets)
                stats["connects"] += 1
                stats["endpoint"] = version
                seed = asyncio.create_task(
                    _seed_books(markets, on_book, since=time.time()))
                try:
                    await _wallex_read(ws, version, on_book, on_trades, stats, stop)
                finally:
                    seed.cancel()
        except _NETWORK_ERRORS as error:
            stats["errors"] += 1
            log.warning("wallex %s feed error: %s", version, describe_error(error))
        if stats["msgs"] > msgs_before:
            pause = 1.0
        else:
            endpoint = (endpoint + 1) % len(WALLEX_ENDPOINTS)
        if not stop.is_set():
            await asyncio.sleep(pause)
            pause = min(30.0, pause * 2)


async def _wallex_read(ws, version: str, on_book: Callable, on_trades: Callable,
                       stats: dict, stop: asyncio.Event) -> None:
    """Reads Wallex frames and sends each message to its callback.

    A version 2 frame can hold several messages, one per line. A version 1
    frame holds one message.
    """
    parse = parse_wallex if version == "v2" else parse_wallex_v1
    while not stop.is_set():
        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_S)
        now = time.time()
        for line in (raw.split("\n") if version == "v2" else [raw]):
            parsed = parse(line) if line else None
            if parsed is None:
                continue
            if parsed[0] == "ping":
                await ws.send("{}")
                continue
            stats["msgs"] += 1
            stats["last"] = now
            if parsed[0] == "book":
                on_book(parsed[1], parsed[2], parsed[3], now, None)
            else:
                on_trades(parsed[1], parsed[2], now)


async def run_wallex(*, markets: list[str], on_book: Callable,
                     on_trades: Callable, stats: dict, stop: asyncio.Event) -> None:
    """Runs as many Wallex connections as the channel limit needs.

    Args:
        markets: Wallex symbols such as "BTCTMN".
        on_book: Called as on_book(market, side, levels, now, seed_since).
        on_trades: Called as on_trades(market, trades, now).
        stats: The stats dict of the Wallex feed.
        stop: The event that ends the feed.
    """
    size = WALLEX_MARKETS_PER_CONNECTION
    groups = [markets[i:i + size] for i in range(0, len(markets), size)]
    await asyncio.gather(*(
        _wallex_connection(group, on_book, on_trades, stats, stop)
        for group in groups))


async def _run_quote_ws(*, venue: str, url: Callable, subscribe: list[str],
                        parse: Callable, ping: Callable | None, ping_every_s: float,
                        on_quote: QuoteCallback, stats: dict,
                        stop: asyncio.Event) -> None:
    """Keeps one global quote websocket alive until `stop`.

    Args:
        venue: The venue name in the stats and the quotes.
        url: An async function that returns the websocket URL.
        subscribe: The messages to send after the connect.
        parse: A parser that returns (coin, bid, ask) or None.
        ping: A function that returns an application ping, or None.
        ping_every_s: The time between application pings.
        on_quote: Called as on_quote(venue, coin, bid, ask, now).
        stats: The stats dict of this venue.
        stop: The event that ends the feed.
    """
    pause = 1.0
    while not stop.is_set():
        try:
            async with connect(await url(), proxy=None, open_timeout=15,
                               max_size=2**23) as ws:
                for message in subscribe:
                    await ws.send(message)
                stats["connects"] += 1
                pause = 1.0
                last_message = last_ping = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except asyncio.TimeoutError:
                        raw = None
                    now = time.time()
                    if ping is not None and now - last_ping >= ping_every_s:
                        await ws.send(ping())
                        last_ping = now
                    if raw is None:
                        if now - last_message > SILENCE_S:
                            raise asyncio.TimeoutError(f"{venue} silent")
                        continue
                    quote = parse(raw) if isinstance(raw, str) else None
                    if quote is None:
                        continue
                    last_message = now
                    stats["msgs"] += 1
                    stats["last"] = now
                    on_quote(venue, quote[0], quote[1], quote[2], now)
        except _NETWORK_ERRORS as error:
            stats["errors"] += 1
            log.warning("%s feed error: %s", venue, describe_error(error))
        if not stop.is_set():
            await asyncio.sleep(pause)
            pause = min(30.0, pause * 2)


async def _fixed_url(value: str) -> str:
    """Returns `value`. It is the URL function of venues with a fixed URL."""
    return value


async def run_bybit(*, coins: list[str], on_quote: QuoteCallback, stats: dict,
                    stop: asyncio.Event) -> None:
    """Runs the Bybit spot best-quote feed (orderbook.1)."""
    topics = [f"orderbook.1.{coin}USDT" for coin in coins]
    # Bybit spot accepts at most 10 topics in one subscribe request.
    subscribe = [json.dumps({"op": "subscribe", "args": topics[i:i + 10]})
                 for i in range(0, len(topics), 10)]
    await _run_quote_ws(
        venue="bybit",
        url=lambda: _fixed_url("wss://stream.bybit.com/v5/public/spot"),
        subscribe=subscribe, parse=parse_bybit,
        ping=lambda: json.dumps({"op": "ping"}), ping_every_s=20.0,
        on_quote=on_quote, stats=stats, stop=stop)


async def run_okx(*, coins: list[str], on_quote: QuoteCallback, stats: dict,
                  stop: asyncio.Event) -> None:
    """Runs the OKX tick-by-tick best bid and offer feed (bbo-tbt)."""
    args = [{"channel": "bbo-tbt", "instId": f"{coin}-USDT"} for coin in coins]
    await _run_quote_ws(
        venue="okx", url=lambda: _fixed_url("wss://ws.okx.com:8443/ws/v5/public"),
        subscribe=[json.dumps({"op": "subscribe", "args": args})],
        parse=parse_okx, ping=lambda: "ping", ping_every_s=25.0,
        on_quote=on_quote, stats=stats, stop=stop)


async def _kucoin_url() -> str:
    """Gets a public KuCoin websocket token and returns the connect URL."""
    bullet = await asyncio.to_thread(
        _http_json, "https://api.kucoin.com/api/v1/bullet-public", b"")
    server = bullet["data"]["instanceServers"][0]
    return (f"{server['endpoint']}?token={bullet['data']['token']}"
            f"&connectId={uuid.uuid4().hex}")


async def run_kucoin(*, coins: list[str], on_quote: QuoteCallback, stats: dict,
                     stop: asyncio.Event) -> None:
    """Runs the KuCoin ticker feed, which carries the best bid and ask."""
    topic = "/market/ticker:" + ",".join(f"{coin}-USDT" for coin in coins)
    await _run_quote_ws(
        venue="kucoin", url=_kucoin_url,
        subscribe=[json.dumps({"id": "1", "type": "subscribe", "topic": topic,
                               "response": True})],
        parse=parse_kucoin,
        ping=lambda: json.dumps({"id": uuid.uuid4().hex, "type": "ping"}),
        ping_every_s=18.0, on_quote=on_quote, stats=stats, stop=stop)


async def run_gate(*, coins: list[str], on_quote: QuoteCallback, stats: dict,
                   stop: asyncio.Event) -> None:
    """Runs the Gate spot.book_ticker feed."""
    subscribe = json.dumps({
        "time": int(time.time()), "channel": "spot.book_ticker",
        "event": "subscribe", "payload": [f"{coin}_USDT" for coin in coins]})
    await _run_quote_ws(
        venue="gate", url=lambda: _fixed_url("wss://api.gateio.ws/ws/v4/"),
        subscribe=[subscribe], parse=parse_gate,
        ping=lambda: json.dumps({"time": int(time.time()), "channel": "spot.ping"}),
        ping_every_s=20.0, on_quote=on_quote, stats=stats, stop=stop)


async def run_binance(*, coins: list[str], on_quote: QuoteCallback, stats: dict,
                      stop: asyncio.Event) -> None:
    """Runs the Binance spot bookTicker feed (market-data endpoint, port 443).

    The streams are in the URL, so the feed sends no subscribe message. The
    server sends websocket ping frames, and the library answers them.
    """
    streams = "/".join(f"{coin.lower()}usdt@bookTicker" for coin in coins)
    await _run_quote_ws(
        venue="binance",
        url=lambda: _fixed_url(f"wss://data-stream.binance.vision/stream?streams={streams}"),
        subscribe=[], parse=parse_binance, ping=None, ping_every_s=0.0,
        on_quote=on_quote, stats=stats, stop=stop)


QUOTE_RUNNERS = {"bybit": run_bybit, "okx": run_okx, "kucoin": run_kucoin,
                 "gate": run_gate, "binance": run_binance}
