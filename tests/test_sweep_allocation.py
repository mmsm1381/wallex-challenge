"""Tests for the allocation of the bids over all markets."""

from sweepbot import allocation

RANKING = ["ADATMN", "BTCTMN", "ETHUSDT", "TRXTMN", "ETHTMN", "PEPEUSDT",
           "DOGETMN", "XRPUSDT"]
CAPS = {"TMN": 3, "USDT": 2}


def run(ineligible=None, busy=None, now=100.0):
    """Runs one allocation with a switch time of 60 s."""
    return allocation.allocate(ranking=RANKING, caps=CAPS, ineligible=ineligible or {},
                               busy=busy or {}, now=now, switch_min_s=60.0)


def test_best_eligible_markets_get_the_places_and_the_rest_has_no_funds() -> None:
    """All markets eligible: the 3 best toman and the 2 best USDT markets bid.

    Expected: ETHTMN and DOGETMN are skipped for missing funds.
    """
    # when
    result = run()

    # then
    assert result.selected == ["ADATMN", "BTCTMN", "TRXTMN", "ETHUSDT", "PEPEUSDT"]
    assert result.no_funds == ["ETHTMN", "DOGETMN", "XRPUSDT"]


def test_ineligible_market_gives_its_place_to_the_next_market() -> None:
    """ADATMN has no fair value (wide spread): ETHTMN takes its place."""
    # when
    result = run(ineligible={"ADATMN": "no_fair_value"})

    # then
    assert result.selected[:3] == ["BTCTMN", "TRXTMN", "ETHTMN"]
    assert "ADATMN" not in result.no_funds


def test_position_keeps_its_funds_and_uses_a_place() -> None:
    """DOGETMN holds coins. Only 2 toman places are left for new bids."""
    # when
    result = run(busy={"DOGETMN": (allocation.HOLDING, 10.0)})

    # then
    assert result.selected[:2] == ["ADATMN", "BTCTMN"]
    assert result.used["TMN"] == ["DOGETMN", "ADATMN", "BTCTMN"]
    assert "TRXTMN" in result.no_funds


def test_young_bid_keeps_its_place_against_a_better_market() -> None:
    """ETHTMN has a bid from t=80. ADATMN becomes eligible again at t=100.

    Scenario:
        - Places: BTCTMN, TRXTMN, ETHTMN have bids. ADATMN ranks first.
    Expected: at t=100 (bid age 20 s) ETHTMN stays. At t=150 (age 70 s)
    ADATMN takes the place of ETHTMN.
    """
    # given
    busy = {"BTCTMN": (allocation.BID, 0.0), "TRXTMN": (allocation.BID, 0.0),
            "ETHTMN": (allocation.BID, 80.0)}

    # when
    early = run(busy=busy, now=100.0)
    late = run(busy=busy, now=150.0)

    # then
    assert early.selected[:3] == ["BTCTMN", "TRXTMN", "ETHTMN"]
    assert "ADATMN" in early.no_funds
    assert late.selected[:3] == ["ADATMN", "BTCTMN", "TRXTMN"]


def test_replay_with_allocation_bids_on_the_only_eligible_market(tmp_path) -> None:
    """Allocation on, all 18 markets ranked, only BTCTMN has data.

    Scenario:
        - The 07:03 BTCTMN sweep fixture. ADATMN ranks first but has no book.
        - The plan list is empty of BTCTMN: the allocation must find it.
    Expected: the BTCTMN bid fills in the sweep, and the panel state lists
    ADATMN as ineligible (the fixture has no ADA quotes).
    """
    # given
    import asyncio
    from sweepbot import config
    from sweepbot import runner as runner_mod
    from sweepbot import store as store_mod
    from test_sweep_replay import sweep_records
    cfg = config.load_config(None)
    cfg["plan"]["markets"] = ["ETHTMN"]
    cfg["premium"]["min_samples"] = 12
    cfg["bot"]["out_dir"] = str(tmp_path)
    cfg["allocation"]["enabled"] = True
    run = runner_mod.Runner(cfg, mode="replay", store=store_mod.Store(":memory:"),
                            start_ts=0.0, with_twin=False)

    # when
    asyncio.run(runner_mod.replay(run, sweep_records()))

    # then
    trades = run.store.rows("trades")
    assert [t["market"] for t in trades] == ["BTCTMN"]
    assert run.bot.allocation["ineligible"]["ADATMN"] == "global_stale"  # no ADA quotes
