"""Tests for the sweep bot config: defaults, TOML merge and checks."""

import pytest

from sweepbot import config


def test_load_config_without_file_gives_research_defaults() -> None:
    """The default plan is the out-of-sample plan of RESEARCH.md section 3.2."""
    # given
    path = None

    # when
    cfg = config.load_config(path)

    # then
    assert cfg["plan"]["depth_pct_tmn"] == 1.3
    assert cfg["plan"]["depth_pct_usdt"] == 1.0
    assert cfg["limits"]["max_usdt_share"] == 0.40
    assert cfg["plan"]["markets"] == ["ADATMN", "ETHUSDT", "TRXTMN", "ETHTMN", "PEPEUSDT"]
    assert cfg["limits"]["max_slots"] == 5
    assert cfg["filters"]["global_drop_pct"] == 0.3
    assert cfg["filters"]["fx_move_pct"] == 0.5
    assert cfg["fees"]["tmn_maker"] == 0.25


def test_load_config_with_partial_file_merges_the_override(tmp_path) -> None:
    """A TOML file changes only the keys that it names."""
    # given
    path = tmp_path / "sweep.toml"
    path.write_text('[plan]\ndepth_pct_tmn = 1.6\n[bot]\nmode = "replay"\n')

    # when
    cfg = config.load_config(str(path))

    # then
    assert cfg["plan"]["depth_pct_tmn"] == 1.6
    assert cfg["plan"]["depth_pct_usdt"] == 1.0
    assert cfg["plan"]["size_tmn"] == 1_000_000.0
    assert cfg["bot"]["mode"] == "replay"
    assert cfg["bot"]["db_path"] == "data/sweepbot.db"


def test_load_config_of_repo_file_is_valid() -> None:
    """The checked-in config.sweep.toml loads without an error."""
    # given
    path = "config.sweep.toml"

    # when
    cfg = config.load_config(path)

    # then
    assert cfg["bot"]["mode"] == "paper"


@pytest.mark.parametrize("toml_text", [
    '[bot]\nmode = "fast"\n',
    "[plan]\ndepth_pct_tmn = 0.8\n",
    "[plan]\ndepth_pct_usdt = 3.0\n",
    "[plan]\nsize_tmn = 2000000.0\n",
    '[plan]\nmarkets = ["BTCTMN", "BTCTMN"]\n',
    '[plan]\nmarkets = ["FOOTMN"]\n',
    '[plan]\nmarkets = ["BTCTMN", "ETHTMN", "XRPTMN", "SOLTMN", "ADATMN", "TRXTMN"]\n',
    "[limits]\nmax_usdt_share = 1.5\n",
    "[ai]\nbudget_usd = -1.0\n",
])
def test_load_config_with_invalid_value_raises(tmp_path, toml_text: str) -> None:
    """A value that breaks a hard limit or a known set stops the load."""
    # given
    path = tmp_path / "bad.toml"
    path.write_text(toml_text)

    # when / then
    with pytest.raises(ValueError):
        config.load_config(str(path))
