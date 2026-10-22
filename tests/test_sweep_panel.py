"""Tests for the panel: controls, state file, payload and HTTP server."""

import asyncio
import http.client
import json
import threading

import pytest

from sweepbot import config
from sweepbot import panel
from sweepbot import runner as runner_mod
from sweepbot import store as store_mod
from test_sweep_replay import sweep_records

NOW = 1_792_700_000.0  # 2026-10-23, Tehran afternoon


def make_runner(tmp_path) -> runner_mod.Runner:
    """Builds a paper runner on BTCTMN with a short premium warm-up."""
    cfg = config.load_config(None)
    cfg["plan"]["markets"] = ["BTCTMN"]
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    return runner_mod.Runner(cfg, mode="paper", store=store_mod.Store(":memory:"),
                             start_ts=0.0, with_twin=False)


def test_write_control_sets_and_clears_the_flags(tmp_path) -> None:
    """pause, then flatten, then resume: only the named flag changes."""
    # given
    path = str(tmp_path / "control.json")

    # when
    panel.write_control(path, "pause", NOW)
    panel.write_control(path, "flatten", NOW)
    after = panel.write_control(path, "resume", NOW)

    # then
    assert after == {"paused": False, "flatten": True}
    assert panel.read_control(path) == after


def test_write_control_with_unknown_action_raises(tmp_path) -> None:
    """Only the four actions of the panel are allowed."""
    # given
    path = str(tmp_path / "control.json")

    # when / then
    with pytest.raises(ValueError):
        panel.write_control(path, "rm -rf", NOW)


def test_payload_without_bot_files_is_stale_and_empty(tmp_path) -> None:
    """A stopped bot: no state file, no database."""
    # given
    paths = {"state_path": str(tmp_path / "state.json"), "db_path": str(tmp_path / "x.db"),
             "control_path": str(tmp_path / "control.json")}

    # when
    payload = panel.build_payload(now=NOW, **paths)

    # then
    assert payload["stale"] is True
    assert payload["trades"] == [] and payload["logs"] == []
    assert payload["control"] == {"paused": False, "flatten": False}


def test_payload_has_todays_trades_and_nets_per_bot(tmp_path) -> None:
    """Today's trades of the bot and the twin. Yesterday's trade is not counted."""
    # given
    db_path = str(tmp_path / "bot.db")
    store = store_mod.Store(db_path)
    for ts, mode, net in ((NOW - 60, "live", 8000.0), (NOW - 30, "twin", 5000.0),
                          (NOW - 86400, "live", 99999.0)):
        store.add_trade(ts=ts, market="BTCTMN", mode=mode, entry_fill_ids=[], exit_fill_ids=[],
                        entry_price=1.0, exit_price=1.01, qty=1.0, net_quote=net, net_tmn=net,
                        net_pct=0.8, hold_s=3.0, exit_reason="target")
    store.add_log(ts=NOW - 5, kind="trade", text="BTCTMN: bought", fields={})
    store.close()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"ts": NOW - 1, "mode": "live", "kpi": {"equity": 5_008_000.0}}))

    # when
    payload = panel.build_payload(state_path=str(state_path), db_path=db_path,
                                  control_path=str(tmp_path / "c.json"), now=NOW)

    # then
    assert payload["stale"] is False
    assert [t["mode"] for t in payload["trades"]] == ["twin", "live"]
    kpi = payload["kpi"]
    assert (kpi["net_today_bot"], kpi["net_today_twin"], kpi["trades_today"]) == (8000.0, 5000.0, 1)
    assert kpi["equity"] == 5_008_000.0
    assert payload["logs"][0]["text"] == "BTCTMN: bought"


def test_runner_state_shows_the_live_bid(tmp_path) -> None:
    """After 200 s of a calm market, the BTCTMN bid is live 1.3% below fair value."""
    # given
    runner = make_runner(tmp_path)
    records = [r for r in sweep_records() if r[0] < 190.0]
    asyncio.run(runner_mod.replay(runner, records))

    # when
    runner.write_state(str(tmp_path / "state.json"), 190.0)
    state = json.loads((tmp_path / "state.json").read_text())

    # then
    slot = state["slots"][0]
    assert (slot["market"], slot["state"], slot["order_side"]) == ("BTCTMN", "bid_live", "buy")
    assert slot["order_price"] == pytest.approx(slot["fv"] * 0.987, rel=1e-3)
    assert state["control"] == {"paused": False, "flatten": False}
    assert state["kpi"]["open_slots"] == 1


def test_pause_from_the_panel_cancels_the_bid(tmp_path) -> None:
    """Control file paused -> the bot cancels its bid and logs the change."""
    # given
    runner = make_runner(tmp_path)
    records = [r for r in sweep_records() if r[0] < 190.0]
    asyncio.run(runner_mod.replay(runner, records))
    control = str(tmp_path / "control.json")
    panel.write_control(control, "pause", 190.0)

    # when
    runner.apply_control(control, 190.0)
    asyncio.run(runner.tick(190.1))
    asyncio.run(runner.tick(191.0))

    # then
    assert runner.bot.paused is True
    assert runner.bot.slots == {} or runner.bot.slots["BTCTMN"].state == "idle"
    logs = runner.store.rows("logs", "kind = ?", ("control",))
    assert "bids paused" in logs[0]["text"]


def test_flatten_from_the_panel_cuts_the_position(tmp_path) -> None:
    """After the sweep fill, "sell all" sells the coins at once with a taker order."""
    # given
    runner = make_runner(tmp_path)
    records = [r for r in sweep_records(end_s=230.0, rebound=False) if r[0] < 201.0]
    asyncio.run(runner_mod.replay(runner, records))
    assert runner.bot.slots["BTCTMN"].has_position()
    control = str(tmp_path / "control.json")
    panel.write_control(control, "flatten", 201.0)

    # when
    runner.apply_control(control, 201.0)
    rest = [r for r in sweep_records(end_s=230.0, rebound=False) if r[0] >= 201.0]
    asyncio.run(runner_mod.replay(runner, rest))

    # then
    trades = runner.store.rows("trades")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "flatten"


def test_http_server_serves_state_and_guards_controls(tmp_path) -> None:
    """GET /api/state works. POST /api/control needs a JSON content type."""
    # given
    handler = panel.make_handler(state_path=str(tmp_path / "s.json"), db_path=str(tmp_path / "x.db"),
                                 control_path=str(tmp_path / "c.json"))
    server = panel.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()

    try:
        # when
        state_status, state_body = request("GET", "/api/state")
        form_status, _ = request("POST", "/api/control", body="action=flatten",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        json_status, json_body = request("POST", "/api/control", body=json.dumps({"action": "pause"}),
                                         headers={"Content-Type": "application/json"})
        bad_status, _ = request("POST", "/api/control", body=json.dumps({"action": "boom"}),
                                headers={"Content-Type": "application/json"})
    finally:
        server.shutdown()
        server.server_close()

    # then
    assert state_status == 200 and json.loads(state_body)["stale"] is True
    assert form_status == 415
    assert json_status == 200 and json.loads(json_body)["control"]["paused"] is True
    assert bad_status == 400
    assert panel.read_control(str(tmp_path / "c.json")) == {"paused": True, "flatten": False}
