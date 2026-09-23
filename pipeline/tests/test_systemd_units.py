from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_linux_monitoring_units_use_once_and_expected_intervals():
    realtime = (ROOT / "deploy/systemd/pdftotex-realtime-monitor.service").read_text()
    realtime_timer = (ROOT / "deploy/systemd/pdftotex-realtime-monitor.timer").read_text()
    daily = (ROOT / "deploy/systemd/pdftotex-daily-stats.service").read_text()
    daily_timer = (ROOT / "deploy/systemd/pdftotex-daily-stats.timer").read_text()

    assert "worker_watch.py once" in realtime
    assert "OnUnitActiveSec=120s" in realtime_timer
    assert "daily_stats.py once" in daily
    assert "OnUnitActiveSec=600s" in daily_timer
    assert "--scheduled" not in realtime
