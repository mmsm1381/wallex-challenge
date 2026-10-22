"""Tests for config loading and fee rates."""

from lagcatcher import config


def test_fee_rates_for_tmn_returns_challenge_account_fractions() -> None:
    """The challenge account pays 0.25% maker and 0.30% taker on toman markets."""
    # given
    cfg = config.load_config(path=None)

    # when
    maker, taker = config.fee_rates(quote="TMN", cfg=cfg)

    # then
    assert (maker, taker) == (0.0025, 0.003)


def test_fee_rates_for_usdt_returns_challenge_account_fractions() -> None:
    """The challenge account pays 0.10% maker and 0.13% taker on USDT markets."""
    # given
    cfg = config.load_config(path=None)

    # when
    maker, taker = config.fee_rates(quote="USDT", cfg=cfg)

    # then
    assert (maker, taker) == (0.001, 0.0013)


def test_load_config_with_partial_file_keeps_other_defaults(tmp_path) -> None:
    """A file that sets one key changes only that key in its section."""
    # given
    path = tmp_path / "config.toml"
    path.write_text('[paper]\nnotional_tmn = 500000.0\n')

    # when
    cfg = config.load_config(path=str(path))

    # then
    assert cfg["paper"]["notional_tmn"] == 500000.0
    assert cfg["paper"]["min_fill_fraction"] == 0.5
    assert cfg["fees"]["tmn_taker"] == 0.30
