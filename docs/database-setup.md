# PostgreSQL & TimescaleDB Setup Guide

This comprehensive guide walks you through setting up **PostgreSQL** and the **TimescaleDB** extension for the Empyrean V2 backend across different environments (Docker, Windows, WSL2, Linux, macOS, and Cloud).

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Requirements](#-requirements)
- [Installation Methods](#-installation-methods)
  - [Method 1: Docker (Recommended / Fastest)](#method-1-docker-recommended--fastest)
  - [Method 2: Windows (WSL2 / Native)](#method-2-windows-wsl2--native)
  - [Method 3: Linux (Ubuntu / Debian)](#method-3-linux-ubuntu--debian)
  - [Method 4: macOS (Homebrew)](#method-4-macos-homebrew)
  - [Method 5: Managed Cloud Services](#method-5-managed-cloud-services)
- [Database Creation & `.env` Configuration](#-database-creation---env-configuration)
- [Running Migrations (Alembic)](#-running-migrations-alembic)
- [Seeding Initial Data](#-seeding-initial-data)
- [Verifying TimescaleDB & Hypertable Status](#-verifying-timescaledb--hypertable-status)
- [Troubleshooting & FAQs](#-troubleshooting--faqs)

---

## 💡 Overview

Empyrean relies on **PostgreSQL** as its primary relational store and **TimescaleDB** (a time-series extension for PostgreSQL) for:
- **Hypertables**: Partitioned time-series tables (`sensor_readings`) chunked by time interval (7-day default chunks) for ultra-fast queries and bulk telemetry ingestion.
- **Analytical Functions**: `time_bucket()` aggregations used across `/api/v1/readings/history` and Celery background aggregation tasks.
- **Data Lifecycle**: Future-ready compression and automated data retention policies.

---

## ⚙️ Requirements

| Component | Minimum Version | Recommended |
|-----------|-----------------|-------------|
| **PostgreSQL** | `14+` | `16+` or `17+` |
| **TimescaleDB** | `2.x+` | Latest stable (matching PG version) |
| **Python** | `3.10+` | `3.12+` |

---

## 🚀 Installation Methods

Choose the installation option that matches your workflow:

### Method 1: Docker (Recommended / Fastest)

The fastest and most isolated way to run PostgreSQL with TimescaleDB pre-configured is using the official Timescale Docker image.

#### Quick Run with Docker CLI:
```bash
docker run -d \
  --name empyrean-timescaledb \
  -p 5432:5432 \
  -e POSTGRES_DB=Empyrean \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -v empyrean_pgdata:/var/lib/postgresql/data \
  timescale/timescaledb-ha:pg16-latest
```

#### Or Using `docker-compose.yml`:
Create or add to your `docker-compose.yml`:
```yaml
version: '3.8'

services:
  timescaledb:
    image: timescale/timescaledb-ha:pg16-latest
    container_name: empyrean-timescaledb
    restart: unless-stopped
    environment:
      POSTGRES_DB: Empyrean
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

Start the container:
```bash
docker compose up -d timescaledb
```

---

### Method 2: Windows (WSL2 / Native)

#### Option A: Inside WSL2 (Ubuntu) — Recommended for Windows Developers
Since Redis and Celery workflows in Empyrean work smoothly under WSL2:

1. Open WSL terminal (e.g. `wsl`):
   ```bash
   # Add TimescaleDB PPA
   sudo apt update
   sudo apt install -y gnupg lsb-release wget
   echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -c -s) main" | sudo tee /etc/apt/sources.list.d/timescaledb.list
   wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg
   sudo apt update

   # Install PostgreSQL + TimescaleDB for PostgreSQL 16
   sudo apt install -y postgresql-16 timescaledb-2-postgresql-16

   # Tune configuration to preload TimescaleDB
   sudo timescaledb-tune --yes

   # Start PostgreSQL service
   sudo service postgresql start
   ```

2. Set default postgres password & create database:
   ```bash
   sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"
   sudo -u postgres psql -c "CREATE DATABASE \"Empyrean\";"
   ```

#### Option B: Native Windows Installer
1. Download and run the **PostgreSQL for Windows Installer** (PostgreSQL 16 or 17) from [EnterpriseDB / postgresql.org](https://www.postgresql.org/download/windows/).
2. Download the matching **TimescaleDB Windows zip package** from the [Timescale Releases page](https://github.com/timescale/timescaledb/releases).
3. Run `setup.exe` from the unzipped TimescaleDB package pointing to your PostgreSQL installation path (e.g., `C:\Program Files\PostgreSQL\16`).
4. Ensure `shared_preload_libraries = 'timescaledb'` is included in your `C:\Program Files\PostgreSQL\16\data\postgresql.conf`.
5. Restart the PostgreSQL Windows Service (`services.msc` -> restart `postgresql-x64-16`).

> 🎥 **Video Tutorial:**  
> For a visual step-by-step walkthrough on installing and configuring TimescaleDB on PostgreSQL, watch this YouTube tutorial: [How to Install TimescaleDB (YouTube)](https://youtu.be/KlOGfFzLdqA).

---

### Method 3: Linux (Ubuntu / Debian)

1. Add Timescale repository and install:
   ```bash
   sudo apt update && sudo apt install -y gnupg lsb-release wget
   echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -c -s) main" | sudo tee /etc/apt/sources.list.d/timescaledb.list
   wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg
   sudo apt update
   sudo apt install -y postgresql-16 timescaledb-2-postgresql-16
   ```

2. Configure preload library:
   ```bash
   sudo timescaledb-tune --yes
   sudo systemctl restart postgresql
   ```

---

### Method 4: macOS (Homebrew)

1. Install via Homebrew:
   ```bash
   brew tap timescale/tap
   brew install timescaledb
   ```

2. Configure and start:
   ```bash
   timescaledb-tune --yes
   brew services start postgresql@16
   ```

---

### Method 5: Managed Cloud Services

If you are using managed cloud databases:
- **Timescale Cloud**: Native TimescaleDB support with full cloud scale.
- **AWS RDS / Aurora PostgreSQL**: Supports TimescaleDB (requires selecting parameter group with `shared_preload_libraries = timescaledb`).
- **Supabase / Aiven**: Enable the `timescaledb` extension under database extensions in the console.

---

## 🗄️ Database Creation & `.env` Configuration

### 1. Create the Database

Create the target database (named `Empyrean` by default):

```bash
# Using psql
psql -U postgres -h localhost -p 5432 -c "CREATE DATABASE \"Empyrean\";"
```

> ⚠️ **Case Sensitivity Note:**
> The database name `Empyrean` contains uppercase letters. When creating it in raw SQL, use quotes (`"Empyrean"`) so PostgreSQL preserves the casing.

### 2. Configure `.env` Connection URL

Ensure your `.env` contains the correct credentials:

```env
# Format: postgresql://<user>:<password>@<host>:<port>/<dbname>
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/Empyrean
```

If your password contains special characters (e.g. `@`, `:`, `/`, `#`), ensure they are URL-encoded (e.g. `@` -> `%40`).

---

## ⚡ Running Migrations (Alembic)

Empyrean uses **Alembic** to manage database schema versions and extension creation.

The migration sequence:
1. `0001_initial_schema.py`: Creates core tables (`users`, `nodes`, `sensor_readings`, `hourly_agg`, `alerts`, `system_settings`, `refresh_tokens`).
2. `0002_add_timescaledb_hypertable.py`: Runs `CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE` and transforms `sensor_readings` into a TimescaleDB hypertable with 7-day chunking on the `time` column.
3. `0003_add_alerts_partial_unique.py`: Adds active alert deduplication indexes.
4. `0004_add_server_defaults.py`: Ensures PostgreSQL-level default timestamps and flags.
5. `0005_add_audit_logs.py`: Creates the `audit_logs` table backing the admin settings audit trail.
6. `0006_add_alert_constraints.py`: Adds alert hardening — `CHECK (LENGTH(message) <= 10000)`, the partial `(triggered_at DESC) WHERE unacked` index, and `refresh_tokens.token_hash` narrowed to `VARCHAR(64)`.
7. `0007_add_refresh_token_expiry_index.py`: Adds the `(expires_at)` index on `refresh_tokens` so expiry sweeps stop seq-scanning.

### Apply Migrations:

```bash
# With active virtual environment (.venv)
alembic upgrade head
```

Or using the helper script:
```bash
# Linux / macOS / Git Bash
./scripts/db.sh migrate
```

---

## 🌱 Seeding Initial Data

Populate the database with system settings, default admin credentials, and initial sensor nodes:

```bash
python scripts/seed.py
```

### Seeded Defaults:
- **Admin Account**: there is no hardcoded admin. Set `BOOTSTRAP_ADMIN_USERNAME` /
  `BOOTSTRAP_ADMIN_PASSWORD` (optionally `BOOTSTRAP_ADMIN_EMAIL`) in `.env` before
  seeding; the seeder creates that user with the `admin` role. The password must
  pass the strength gate (≥ 8 chars, mixed case, digit, symbol).
- **Default Node**:
  - **Node ID**: `ESP32-01`
  - **Location**: `Lab 1 - Central Monitoring`
- **System Settings**:
  - `aqi_warning_threshold`: `100`
  - `aqi_critical_threshold`: `150`
  - `data_retention_days`: `365`
  - `alerts_enabled`: `true`

---

## 🔍 Verifying TimescaleDB & Hypertable Status

### 1. Run Pre-flight Health Check
```bash
python scripts/check_health.py
```

Look for the database and hypertable sections:
```text
[2/9] Database
------------------------------------------------------------
  [OK]  PostgreSQL connection  -  localhost:5432/Empyrean
  [OK]  PostgreSQL version  -  PostgreSQL 16.x ...

[3/9] Tables
------------------------------------------------------------
  [OK]  All 7 tables exist  -  alerts, hourly_agg, nodes, refresh_tokens, sensor_readings, system_settings, users

[6/9] Hypertable
------------------------------------------------------------
  [OK]  sensor_readings is a hypertable  -  sensor_readings
```

### 2. Run Stack Verification Script
```bash
python scripts/verify.py
```

### 3. Direct SQL Inspection
Open a psql session:
```bash
# Via db.sh helper:
./scripts/db.sh connect

# Or standard psql:
psql -U postgres -d Empyrean
```

Run TimescaleDB metadata queries:
```sql
-- Check installed extension version
SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';

-- Check hypertable configuration
SELECT hypertable_schema, hypertable_name, num_dimensions, num_chunks 
FROM timescaledb_information.hypertables 
WHERE hypertable_name = 'sensor_readings';

-- Check hypertable chunks
SELECT chunk_name, primary_dimension, range_start, range_end 
FROM timescaledb_information.chunks 
WHERE hypertable_name = 'sensor_readings';
```

Or run via the CLI helper:
```bash
./scripts/db.sh hypertables
```

---

## 🛠️ Troubleshooting & FAQs

### Q1: `FATAL: extension "timescaledb" must be preloaded`
**Cause:** PostgreSQL started without loading the TimescaleDB library in memory.  
**Fix:**
1. Locate `postgresql.conf` (e.g. `/etc/postgresql/16/main/postgresql.conf` on Linux or `C:\Program Files\PostgreSQL\16\data\postgresql.conf` on Windows).
2. Ensure this line is present and uncommented:
   ```conf
   shared_preload_libraries = 'timescaledb'
   ```
3. Alternatively run `timescaledb-tune --yes`.
4. Restart your PostgreSQL service.

---

### Q2: `FATAL: database "Empyrean" does not exist`
**Cause:** The target database has not been created yet or casing was dropped.  
**Fix:**
Create the database with double quotes to preserve case:
```bash
psql -U postgres -c "CREATE DATABASE \"Empyrean\";"
```

---

### Q3: `password authentication failed for user "postgres"`
**Cause:** Incorrect password in `.env` `DATABASE_URL`.  
**Fix:**
Reset the postgres user password:
```bash
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'your_password';"
```
Then update `DATABASE_URL` in `.env`.

---

### Q4: `alembic.util.exc.CommandError: Can't locate revision identified by 'xxxx'`
**Cause:** Database has a revision ID not present in your local branch.  
**Fix:**
Verify your migrations directory or reset dev database if in a clean local environment:
```bash
alembic current
alembic upgrade head
```

---

## 📚 Related Documentation & Resources
 
- [Database Schema Reference](database.md) — Detailed table, column, and index definitions
- [Getting Started Guide](getting-started.md) — Full stack development workflow
- [Architecture](architecture.md) — High-level telemetry and ingestion architecture
- [System Overview](overview.md) — Full platform deep-dive
- 🎥 [TimescaleDB Installation Video Tutorial (YouTube)](https://youtu.be/KlOGfFzLdqA)
