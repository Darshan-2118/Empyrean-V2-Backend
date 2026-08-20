# Empyrean-V2-Backend

Docs: [docs](docs)

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

pip install -r requirements.txt

# Start Redis
redis-server

# Run migrations
alembic upgrade head

# Seed data
python scripts/seed.py

# Start Celery worker
celery -A celery_app worker --loglevel=info

# Start Celery beat
celery -A celery_app beat --loglevel=info

# Start API
hypercorn app:app --bind 0.0.0.0:8000
```

```bash
# Health check
curl http://localhost:8000/admin/health
python scripts/check_health.py
```

```bash
# Windows quick start
scripts\dev-up.bat
```