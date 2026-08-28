"""Deployment deliverables gate test (Phase 14).

Validates that all deployment artifacts in deploy/ exist and have the required
structure: three systemd units with [Unit]/[Service]/[Install], a nginx.conf
with a 443 server block and the /metrics restriction, a logrotate config,
an executable deploy.sh bash script, and a .env.production.example with the
required secret keys.
"""

from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"

SYSTEMD_UNITS = (
    "quart-api.service",
    "celery-worker.service",
    "celery-beat.service",
)


def test_systemd_units_exist():
    """All three systemd unit files exist."""
    for name in SYSTEMD_UNITS:
        assert (DEPLOY_DIR / name).is_file(), f"Missing {name}"


def test_systemd_units_have_required_sections():
    """Each .service has [Unit], [Service], [Install] + service context."""
    for name in SYSTEMD_UNITS:
        content = (DEPLOY_DIR / name).read_text()
        assert "[Unit]" in content, f"{name} missing [Unit]"
        assert "[Service]" in content, f"{name} missing [Service]"
        assert "[Install]" in content, f"{name} missing [Install]"
        assert "User=empyrean" in content, f"{name} missing User=empyrean"
        assert "WorkingDirectory=/opt/empyrean" in content, f"{name} missing WorkingDirectory"
        assert "EnvironmentFile=/opt/empyrean/.env" in content, f"{name} missing EnvironmentFile"


def test_systemd_exec_starts():
    """Each unit's ExecStart targets the deploy contract paths."""
    exec_starts = {
        "quart-api.service": ["hypercorn", "app:create_app()", "127.0.0.1:8000"],
        "celery-worker.service": ["celery", "celery_app.celery_app", "worker"],
        "celery-beat.service": ["celery", "celery_app.celery_app", "beat"],
    }
    for name, needles in exec_starts.items():
        content = (DEPLOY_DIR / name).read_text()
        for needle in needles:
            assert needle in content, f"{name} ExecStart missing {needle}"


def test_nginx_conf_exists():
    """nginx.conf exists."""
    assert (DEPLOY_DIR / "nginx.conf").is_file()


def test_nginx_conf_has_server_block():
    """nginx.conf has a 443 server block, /ws upgrade, and /metrics restriction."""
    content = (DEPLOY_DIR / "nginx.conf").read_text()
    assert "server {" in content
    assert "listen 443 ssl" in content
    assert "proxy_pass http://127.0.0.1:8000" in content
    assert "/ws" in content and "upgrade" in content
    assert "client_max_body_size 64k" in content
    assert "location /metrics" in content
    assert "allow 127.0.0.1" in content
    assert "deny all" in content


def test_logrotate_exists():
    """logrotate config exists."""
    assert (DEPLOY_DIR / "logrotate").is_file()


def test_deploy_sh_exists_and_executable():
    """deploy.sh exists, is bash, and is strict-mode."""
    path = DEPLOY_DIR / "deploy.sh"
    assert path.is_file()
    content = path.read_text()
    assert content.splitlines()[0] == "#!/usr/bin/env bash"
    assert "set -euo pipefail" in content


def test_env_production_example_exists():
    """.env.production.example exists."""
    assert (DEPLOY_DIR / ".env.production.example").is_file()


def test_env_example_has_required_keys():
    """.env.production.example contains the required secret keys."""
    content = (DEPLOY_DIR / ".env.production.example").read_text()
    required = ("SECRET_KEY", "JWT_SECRET", "DATABASE_URL", "REDIS_URL", "MQTT_BROKER_HOST")
    for key in required:
        assert key in content, f".env.production.example missing {key}"


def test_celery_beat_runtime_directory():
    """M103: no unprivileged ExecStartPre; RuntimeDirectory creates /run/empyrean."""
    content = (DEPLOY_DIR / "celery-beat.service").read_text()
    directives = [l.strip() for l in content.splitlines() if not l.strip().startswith("#")]
    assert not any(d.startswith("ExecStartPre") for d in directives), \
        "celery-beat.service still has an ExecStartPre directive"
    assert "RuntimeDirectory=empyrean" in content
    # The flock pidfile/lock path must stay consistent with the runtime dir.
    assert "/run/empyrean/celery-beat.lock" in content


def test_units_have_start_limits():
    """L74: all three units bound their restart loop via StartLimit*."""
    for name in SYSTEMD_UNITS:
        content = (DEPLOY_DIR / name).read_text()
        unit_section = content.split("[Service]")[0]
        assert "StartLimitIntervalSec=300" in unit_section, f"{name} missing StartLimitIntervalSec in [Unit]"
        assert "StartLimitBurst=5" in unit_section, f"{name} missing StartLimitBurst in [Unit]"
        assert "Restart=always" in content, f"{name} lost Restart=always"
        assert "RestartSec=5" in content, f"{name} lost RestartSec=5"


def test_deploy_sh_excludes_dev_state():
    """M104: rsync must not ship .celery/ or .venv/ to production."""
    content = (DEPLOY_DIR / "deploy.sh").read_text()
    assert "--exclude .celery" in content
    assert "--exclude .venv" in content
    assert "--exclude venv" in content  # pre-existing exclude kept


def test_deploy_sh_envsubst_required():
    """L75: a missing envsubst must fail the deploy, not silently skip."""
    content = (DEPLOY_DIR / "deploy.sh").read_text()
    assert "envsubst not found" in content
    lines = content.splitlines()
    start = next(i for i, l in enumerate(lines) if "command -v envsubst" in l)
    else_idx = next(i for i in range(start, len(lines)) if lines[i].strip() == "else")
    fi_idx = next(i for i in range(else_idx, len(lines)) if lines[i].strip() == "fi")
    branch = "\n".join(lines[else_idx:fi_idx])
    assert "exit 1" in branch, "envsubst-missing branch must exit 1"


def test_nginx_export_timeout():
    """M105: /api/v1/export gets a 330s read timeout for 300s CSV exports."""
    content = (DEPLOY_DIR / "nginx.conf").read_text()
    assert "location /api/v1/export" in content
    block = content.split("location /api/v1/export")[1].split("}")[0]
    assert "proxy_pass http://127.0.0.1:8000" in block
    assert "proxy_read_timeout 330s" in block