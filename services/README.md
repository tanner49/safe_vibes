# Local Test Services

This folder is intentionally test-only scaffolding.

The shipped demo data source is `demo_data/demo_sales.sqlite3`. Rebuild it with:

```powershell
python services/build_demo_sqlite.py
```

The Postgres service below is optional scaffolding for trying the same kind of
fake SaaS data against a real Postgres warehouse connection.

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
Database: save_vibes_demo
Username: save_vibes_demo
Password: save_vibes_demo
SSL mode: disable
```

Custom connection string:

```text
postgresql+psycopg://save_vibes_demo:save_vibes_demo@localhost:55432/save_vibes_demo?sslmode=disable
```

Reset the dataset by rerunning the seed script. It drops and recreates the demo
tables, but only inside the configured demo database.
