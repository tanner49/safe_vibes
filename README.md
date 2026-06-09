# safe_reports

A Django-based app for creating and deploying AI-generated reports in a safe manner.

## Local Setup

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Copy `.env.example` to `.env` when you want to override local settings.

The app defaults to SQLite for local development. Set `DATABASE_URL` to use Postgres.

Optional warehouse drivers live in `requirements-db-drivers.txt` so the core Django app can stay easy to install while Snowflake and BigQuery dependencies evolve independently.

## Local Demo Data Service

Test-only local services live under `services/`. To run a disposable Postgres
database with fake SaaS sales data:

```powershell
docker compose -f services/docker-compose.postgres.yml up -d
python services/seed_demo_postgres.py
```

See `services/README.md` for the connection details.

## Bootstrap An Organization

Create a Django superuser for local admin access:

```powershell
python manage.py createsuperuser
```

The prompt asks for an email address. The app uses email as the user identity;
the internal Django `username` field is filled from email automatically.

Then open `/admin/` and create:

1. An `Organization`.
2. A `Membership` connecting your Django user to that organization.
3. A product role for that membership: company admin, creator, or viewer.

Company admin is a product role. It is separate from Django staff/superuser access.

If an organization has `sso_required` enabled, normal password login is blocked for non-staff users in that organization. Django staff/superusers can still use password login for bootstrap/admin access.
