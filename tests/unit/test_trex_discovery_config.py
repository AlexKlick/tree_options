"""C5: discovery scan config — fail-closed load + API-safe echo."""

from __future__ import annotations

from pathlib import Path

import pytest

from tree_options.trex.discovery.config import ScanConfig, config_echo, load_scan_config

GOOD = """
underlyings = ["NVDA", "QQQ"]
dte_min = 20
dte_max = 60
widths = [5.0, 10.0]
target_mode = "auto"
client_id = 74
"""


class TestLoadScanConfig:
    def test_happy_path_defaults_apply(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD)
        cfg = load_scan_config(path)
        assert cfg.underlyings == ["NVDA", "QQQ"]
        assert cfg.widths == [5.0, 10.0]
        assert cfg.client_id == 74
        # untouched defaults
        assert cfg.target_mode == "auto"
        assert cfg.min_debit == 0.15
        assert cfg.max_leg_spread_frac == 0.25
        assert cfg.max_candidates_total == 12

    def test_missing_file_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_scan_config(tmp_path / "nope.toml")

    def test_unknown_key_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD + "\nweapons = true\n")
        with pytest.raises(ValueError, match="unknown key"):
            load_scan_config(path)

    def test_bad_target_mode_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD.replace('target_mode = "auto"', 'target_mode = "vibes"'))
        with pytest.raises(ValueError, match="target_mode"):
            load_scan_config(path)

    def test_reserved_client_id_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        # 71 monitor / 72 enter / 77 IbkrTrex library default
        path.write_text(GOOD.replace("client_id = 74", "client_id = 71"))
        with pytest.raises(ValueError, match="reserved clientId"):
            load_scan_config(path)

    def test_inverted_dte_window_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD.replace("dte_min = 20", "dte_min = 90"))
        with pytest.raises(ValueError, match="dte"):
            load_scan_config(path)

    def test_empty_underlyings_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD.replace('underlyings = ["NVDA", "QQQ"]', "underlyings = []"))
        with pytest.raises(ValueError, match="underlyings"):
            load_scan_config(path)

    def test_nonpositive_width_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD.replace("widths = [5.0, 10.0]", "widths = [5.0, 0.0]"))
        with pytest.raises(ValueError, match="width"):
            load_scan_config(path)

    def test_dte_window_accepted_types(self, tmp_path: Path) -> None:
        cfg = ScanConfig(underlyings=["NVDA"])
        assert cfg.dte_min < cfg.dte_max
        assert cfg.reserved_client_ids == {71, 72, 73, 77}


class TestConfigEcho:
    def test_echo_is_json_safe_and_matches_config(self, tmp_path: Path) -> None:
        path = tmp_path / "discovery.toml"
        path.write_text(GOOD)
        cfg = load_scan_config(path)
        echo = config_echo(cfg)
        import json

        assert json.loads(json.dumps(echo)) == echo  # JSON-safe
        assert echo["underlyings"] == ["NVDA", "QQQ"]
        assert echo["target_mode"] == "auto"
        assert "client_id" not in echo  # operational detail, not page data
