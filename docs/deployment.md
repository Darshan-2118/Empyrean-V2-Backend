# Deployment Guide (Phase 14)

Production deployment of the Empyrean backend onto a systemd Linux server. The
deploy artifacts live in `deploy/` — copy the repo to the server and run
`deploy/deploy.sh`, or follow the manual steps below.

## Topology

```
[Internet] → Nginx (443, TLS) → hypercorn (127.0.0.1:8000, app:create_app())
                               → Celery worker (celery_app.celery_app)
                               → Celery beat (celery_app.celery_app)
```

- System user `empyrean`, home `/opt/empyrean`
- Venv at `/opt/empyrean/venv`, config at `/opt/empyrean/.env`
- MQTT TLS certs at `/opt/empyrean/certs/`
- Nginx terminates TLS and proxies `443 → 127.0.0.1:8000`; `/ws` gets the
  WebSocket upgrade headers; `/metrics` is restricted to `127.0.0.1`
- Log rotation via `deploy/logrotate` (included in the systemd journal, sized
  and rotated by the drop-in)

## Prerequisites

- Ubuntu 22.04+ (or any systemd distro) with systemd
- PostgreSQL 14+ with the TimescaleDB extension
- Redis 7+
- Nginx
- Python 3.11+
- Mosquitto (MQTT broker) with TLS certs for device auth
- A domain name pointed at the server (for Let's Encrypt)

## Automated deploy

The one-shot script syncs code, builds the venv, installs systemd units,
configures nginx, creates `.env` (never clobbering an existing one), and runs
`alembic upgrade head`:

```bash
export SERVER_HOST=your-host.example.com      # required
export SERVER_USER=empyrean                   # default empyrean
export APP_DIR=/opt/empyrean                  # default
./deploy/deploy.sh
```

It is idempotent — re-running it only restarts services and re-applies units.

## Manual steps (what deploy.sh automates)

1. **Create the system user + dirs:**

```bash
sudo useradd -r -s /bin/bash -d /opt/empyrean empyrean
sudo mkdir -p /opt/empyrean
sudo chown empyrean:empyrean /opt/empyrean
```

2. **Copy code** (exclude venv/secrets/certs):

```bash
rsync -az --delete \
  --exclude .git --exclude venv --exclude __pycache__ \
  --exclude .pytest_cache --exclude .env --exclude certs \
  ./ empyrean@SERVER:/opt/empyrean
```

3. **Build the venv + install deps:**

```bash
cd /opt/empyrean
python3 -m venv venv
./venv/bin/pip install -r requirements.txt --upgrade
```

4. **Install + start systemd units:**

```bash
sudo install -m 0644 /opt/empyrean/deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quart-api celery-worker celery-beat
```

5. **Configure nginx:**

```bash
sudo cp /opt/empyrean/deploy/nginx.conf /etc/nginx/sites-available/empyrean
sudo ln -sf /etc/nginx/sites-available/empyrean /etc/nginx/sites-enabled/empyrean
sudo nginx -t && sudo systemctl restart nginx
```

Set the real `<your-domain.com>` and its Let's Encrypt cert paths in nginx.conf.
Procure certs with `certbot --nginx -d your-domain.com`.

6. **Log rotation:**

```bash
sudo cp /opt/empyrean/deploy/logrotate /etc/logrotate.d/empyrean
```

7. **Create `.env`** from the template, fill in secrets, chmod 600:

```bash
cp /opt/empyrean/deploy/.env.production.example /opt/empyrean/.env
chmod 600 /opt/empyrean/.env
```

Required secrets: `SECRET_KEY`, `JWT_SECRET`, `DATABASE_URL`, `REDIS_URL`,
`MQTT_BROKER_HOST`, plus the MQTT TLS cert paths.

8. **Run migrations:**

```bash
/opt/empyrean/venv/bin/alembic upgrade head
```

## Verification

```bash
systemctl status quart-api celery-worker celery-beat
curl https://your-domain.com/health          # {"status":"ok", ...}
curl http://127.0.0.1:8000/metrics            # Prometheus text (internal only)
```

## Service management

| Service | Commands |
|---------|----------|
| API | `systemctl {start|stop|restart|status} quart-api` |
| Celery worker | `systemctl {start|stop|restart|status} celery-worker` |
| Celery beat | `systemctl {start|stop|restart|status} celery-beat` |
| Nginx | `systemctl {start|stop|restart|status} nginx` |

Logs: `journalctl -u quart-api -f`, `-u celery-worker -f`, `-u celery-beat -f`;
nginx at `/var/log/nginx/{access,error}.log`.

## Rollback

The deploy script never deletes the previous release's data. To roll back,
check out the prior code, re-run `deploy/deploy.sh` (deps + units re-applied,
`alembic upgrade head` is a no-op if already at head), or simply
`systemctl restart quart-api celery-worker celery-beat`.

## Monitoring

- `GET /health` — public liveness
- `GET /metrics` — Prometheus text exposition (`empyrean_http_requests_total`,
  `empyrean_http_request_duration_seconds`); nginx only allows `127.0.0.1`, so
  point your Prometheus scraper at the host directly or via an internal IP
- `GET /api/v1/admin/health` — per-component health (admin token)