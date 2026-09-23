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


def test_linux_monitoring_installer_grants_state_database_access():
    installer = (ROOT / "scripts/install-systemd-monitoring.sh").read_text()

    assert "command -v setfacl" in installer
    assert 'workers_root="$deployment_root/data/workers"' in installer
    assert 'u:pdftotex:rwx,d:u:pdftotex:rwx' in installer
    assert '-type d -name .state -print0' in installer


def test_overleaf_publisher_runs_as_pdftotex_every_two_minutes():
    service = (ROOT / "deploy/systemd/pdftotex-overleaf-publish.service").read_text()
    timer = (ROOT / "deploy/systemd/pdftotex-overleaf-publish.timer").read_text()

    assert "User=pdftotex" in service
    assert "publish_completed_overleaf.py" in service
    assert "@PDFTOTEX_ROOT@/overleaf/config.json" in service
    assert "HOME=@PDFTOTEX_ROOT@/overleaf/home" in service
    assert "OnUnitActiveSec=120s" in timer
    assert "Persistent=true" in timer


def test_overleaf_installer_reads_token_from_stdin_and_keeps_it_out_of_config():
    installer = (ROOT / "scripts/install-overleaf-publisher.sh").read_text()

    assert "IFS= read -r overleaf_token" in installer
    assert "OVERLEAF_U1_REMOTE" in installer
    assert "OVERLEAF_U3_REMOTE" in installer
    assert "OVERLEAF_U2_REMOTE" not in installer
    assert "enabled_after" in installer
    assert "chmod 0600" in installer
    assert "systemctl enable --now pdftotex-overleaf-publish.timer" in installer
    assert "git clone --depth=1" in installer
    assert "git config --global http.version HTTP/1.1" in installer
    assert "OVERLEAF_GIT_PROXY" in installer
    assert "for attempt in 1 2 3" in installer
    assert "olp_" not in installer


def test_monitoring_installer_does_not_glob_unrelated_pdftotex_units():
    installer = (ROOT / "scripts/install-systemd-monitoring.sh").read_text()

    assert 'deploy/systemd/pdftotex-*.service' not in installer
    assert 'deploy/systemd/pdftotex-*.timer' not in installer
