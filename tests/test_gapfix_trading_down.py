"""Gap-fix (2026-07-03): a bare `python run_trader.py --arm` must NOT bypass the TRADING_DOWN
marker. The wrapper run_trader_service.sh refuses to arm while the marker exists; run_trader.py
now mirrors that guard (_refuse_if_trading_down) so a manual arm is blocked too. Dry-run / read-only
invocations (arm=False) are never blocked."""
import typer
import pytest

import run_trader


def test_arm_blocked_when_marker_present(tmp_path, monkeypatch):
    marker = tmp_path / "TRADING_DOWN"
    marker.write_text("down")
    monkeypatch.setattr(run_trader, "TRADING_DOWN_MARKER", str(marker))
    with pytest.raises(typer.Exit) as ei:
        run_trader._refuse_if_trading_down(arm=True)
    assert ei.value.exit_code == 1


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
