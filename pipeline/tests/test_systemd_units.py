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


def test_linux_monitoring_installation_root_is_configurable():
    installer = (ROOT / "scripts/install-systemd-monitoring.sh").read_text()
    services = [
        (ROOT / "deploy/systemd/pdftotex-realtime-monitor.service").read_text(),
        (ROOT / "deploy/systemd/pdftotex-daily-stats.service").read_text(),
    ]

    assert "PDFTOTEX_ROOT" in installer
    assert "@PDFTOTEX_ROOT@" in installer
    assert all("@PDFTOTEX_ROOT@" in service for service in services)
    assert all("/srv/pdftotex" not in service for service in services)
