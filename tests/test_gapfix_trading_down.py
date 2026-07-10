"""TRADING_DOWN blocks BUYs without disarming protective SELL exits."""

import run_trader


def test_service_start_allowed_for_protective_exits_when_marker_present(tmp_path, monkeypatch):
    marker = tmp_path / "TRADING_DOWN"
    marker.write_text("down")
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    run_trader._refuse_if_trading_down(arm=True)


def test_arm_allowed_when_marker_absent(tmp_path, monkeypatch):
    marker = tmp_path / "TRADING_DOWN"  # not created
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    # No marker -> guard must be a no-op (does not raise), so a legitimate arm can proceed.
    run_trader._refuse_if_trading_down(arm=True)


def test_dry_run_never_blocked_even_with_marker(tmp_path, monkeypatch):
    marker = tmp_path / "TRADING_DOWN"
    marker.write_text("down")
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    # arm=False (dry-run / read-only) must never be blocked, marker present or not.
    run_trader._refuse_if_trading_down(arm=False)
