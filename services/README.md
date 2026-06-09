# Local Test Services

This folder is intentionally test-only scaffolding. Nothing here is imported by the
production Django app.

## Demo Postgres

Start a local Postgres database:

```powershell
docker compose -f services/docker-compose.postgres.yml up -d
```

Create and populate fake SaaS sales data:

```powershell
python services/seed_demo_postgres.py
```

Default connection details:

```text
Host: localhost
Port: 55432
Database: safe_reports_demo
Username: safe_reports_demo
Password: safe_reports_demo
SSL mode: disable
```

Custom connection string:

```text
postgresql+psycopg://safe_reports_demo:safe_reports_demo@localhost:55432/safe_reports_demo?sslmode=disable
```

Reset the dataset by rerunning the seed script. It drops and recreates the demo
tables, but only inside the configured demo database.
