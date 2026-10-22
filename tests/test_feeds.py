"""Tests for the websocket message parsers. The samples are real frames."""

from lagcatcher import feeds


def test_parse_wallex_depth_with_message_wrapper_returns_sorted_bid_levels() -> None:
    """The live server wraps the levels in data.message."""
    # given
    line = ('{"push":{"channel":"orderbook:BTCUSDT@buyDepth","pub":{"data":'
            '{"message":[{"price":84384.61,"quantity":0.0026,"sum":219.4},'
            '{"price":84384.99,"quantity":0.002,"sum":168.77}]}}}}')

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed == ("book", "BTCUSDT", "bid", [(84384.99, 0.002), (84384.61, 0.0026)])


def test_parse_wallex_depth_without_wrapper_returns_ask_levels() -> None:
    """The documentation shows the levels directly in data."""
    # given
    line = ('{"push":{"channel":"orderbook:BTCTMN@sellDepth","pub":{"data":'
            '[{"quantity":255.75,"price":82131,"sum":21005003.25}]}}}')

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed == ("book", "BTCTMN", "ask", [(82131.0, 255.75)])


def test_parse_wallex_trade_dict_returns_one_trade_with_maker_side() -> None:
    """isBuyOrder=true means the buy order was the maker."""
    # given
    line = ('{"push":{"channel":"public:BTCTMN@trade","pub":{"data":'
            '{"isBuyOrder":true,"quantity":"0.000243","price":"15791249224",'
            '"timestamp":"2026-08-25T16:38:23Z"}}}}')

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed == ("trades", "BTCTMN",
                      [(15791249224.0, 0.000243, True, "2026-08-25T16:38:23Z")])


def test_parse_wallex_trade_list_in_wrapper_returns_all_trades() -> None:
    """A wrapped list of trades gives one tuple per trade."""
    # given
    line = ('{"push":{"channel":"public:PEPETMN@trade","pub":{"data":{"message":['
            '{"isBuyOrder":false,"quantity":"10","price":"1.24","timestamp":"t1"},'
            '{"isBuyOrder":true,"quantity":"20","price":"1.23","timestamp":"t2"}]}}}}')

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed == ("trades", "PEPETMN",
                      [(1.24, 10.0, False, "t1"), (1.23, 20.0, True, "t2")])


def test_parse_wallex_ping_returns_ping() -> None:
    """The server ping is an empty object."""
    # given
    line = "{}"

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed == ("ping",)


def test_parse_wallex_subscribe_reply_returns_none() -> None:
    """A command reply has no market data."""
    # given
    line = '{"id":2,"subscribe":{}}'

    # when
    parsed = feeds.parse_wallex(line)

    # then
    assert parsed is None


def test_parse_bybit_level1_snapshot_returns_coin_bid_ask() -> None:
    """A Bybit orderbook.1 snapshot gives the best bid and ask."""
    # given
    text = ('{"topic":"orderbook.1.BTCUSDT","ts":1792603001499,"type":"snapshot",'
            '"data":{"s":"BTCUSDT","b":[["84467.1","0.67"]],"a":[["84467.2","0.51"]],'
            '"u":128833097,"seq":115865578368},"cts":1792603001495}')

    # when
    parsed = feeds.parse_bybit(text)

    # then
    assert parsed == ("BTC", 84467.1, 84467.2)


def test_parse_okx_bbo_returns_coin_bid_ask() -> None:
    """An OKX bbo-tbt push gives the best bid and ask."""
    # given
    text = ('{"arg":{"channel":"bbo-tbt","instId":"BTC-USDT"},"data":[{"asks":'
            '[["84469.6","0.72","0","10"]],"bids":[["84469.5","0.54","0","12"]],'
            '"ts":"1792603001708","seqId":82707167995}]}')

    # when
    parsed = feeds.parse_okx(text)

    # then
    assert parsed == ("BTC", 84469.5, 84469.6)


def test_parse_kucoin_ticker_returns_coin_bid_ask() -> None:
    """A KuCoin /market/ticker push gives the best bid and ask."""
    # given
    text = ('{"topic":"/market/ticker:PEPE-USDT","type":"message","subject":'
            '"trade.ticker","data":{"bestAsk":"0.000004638","bestAskSize":"121",'
            '"bestBid":"0.000004637","bestBidSize":"123","price":"0.000004637",'
            '"time":1792602990856}}')

    # when
    parsed = feeds.parse_kucoin(text)

    # then
    assert parsed == ("PEPE", 0.000004637, 0.000004638)


def test_parse_gate_book_ticker_returns_coin_bid_ask() -> None:
    """A Gate spot.book_ticker update gives the best bid and ask."""
    # given
    text = ('{"time":1792603002,"channel":"spot.book_ticker","event":"update",'
            '"result":{"t":1792603002540,"u":40504014922,"s":"BTC_USDT",'
            '"b":"84464.5","B":"0.44","a":"84464.6","A":"0.02"}}')

    # when
    parsed = feeds.parse_gate(text)

    # then
    assert parsed == ("BTC", 84464.5, 84464.6)


def test_parse_binance_book_ticker_returns_coin_bid_ask() -> None:
    """A Binance combined-stream bookTicker push gives the best bid and ask."""
    # given
    text = ('{"stream":"pepeusdt@bookTicker","data":{"u":40504014922,'
            '"s":"PEPEUSDT","b":"0.00000451","B":"9e9","a":"0.00000452","A":"8e9"}}')

    # when
    parsed = feeds.parse_binance(text)

    # then
    assert parsed == ("PEPE", 0.00000451, 0.00000452)


def test_parse_binance_subscribe_reply_returns_none() -> None:
    """The reply to a subscribe request carries no quote."""
    # given
    text = '{"result":null,"id":1}'

    # when
    parsed = feeds.parse_binance(text)

    # then
    assert parsed is None


def test_parse_wallex_v1_depth_array_returns_sorted_ask_levels() -> None:
    """Version 1 sends [channel, levels] arrays."""
    # given
    text = ('["BTCUSDT@sellDepth",[{"price":84300.9,"quantity":0.1,"sum":8430.09},'
            '{"price":84300.4,"quantity":0.0063,"sum":531.48}]]')

    # when
    parsed = feeds.parse_wallex_v1(text)

    # then
    assert parsed == ("book", "BTCUSDT", "ask", [(84300.4, 0.0063), (84300.9, 0.1)])


def test_parse_wallex_v1_trade_array_returns_one_trade() -> None:
    """A version 1 trade is [channel, trade dict]."""
    # given
    text = ('["PEPETMN@trade",{"isBuyOrder":false,"price":"1.2284",'
            '"quantity":"162813","timestamp":"2026-10-21T19:04:03Z"}]')

    # when
    parsed = feeds.parse_wallex_v1(text)

    # then
    assert parsed == ("trades", "PEPETMN",
                      [(1.2284, 162813.0, False, "2026-10-21T19:04:03Z")])


def test_parse_wallex_v1_connection_info_object_returns_none() -> None:
    """The first version 1 message is a connection info object, not data."""
    # given
    text = ('{"sid":"0952ca11","upgrades":[],"pingInterval":20000,'
            '"pingTimeout":30000,"maxPayload":1048576}')

    # when
    parsed = feeds.parse_wallex_v1(text)

    # then
    assert parsed is None
