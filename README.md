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

## Bundled Demo Data

The repo includes a small read-only SQLite demo warehouse at
`demo_data/demo_sales.sqlite3`. New organizations automatically get an enabled
database connection named `Demo SaaS Sales`, so report builders can try fake
SaaS sales data before connecting a real warehouse.

To refresh the committed demo data file after changing the seed script:

```powershell
python services/build_demo_sqlite.py
```

To backfill or refresh demo connections for existing organizations:

```powershell
python manage.py ensure_demo_database
```

This SQLite file is an external report data source. It is not the Django
application database. In production, Django should use Heroku Postgres via
`DATABASE_URL`.

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

## Heroku Deployment

The app is prepared for Heroku with:

- `Procfile` web process: `gunicorn config.wsgi:application`
- `Procfile` release process: `python manage.py migrate && python manage.py ensure_demo_database`
- `.python-version` set to Python `3.12`
- `DATABASE_URL` support through `dj-database-url`
- WhiteNoise static file serving

Recommended setup:

```powershell
heroku login
heroku create your-app-name
heroku addons:create heroku-postgresql:essential-0 --app your-app-name
heroku config:set DJANGO_DEBUG=false --app your-app-name
heroku config:set DJANGO_SECRET_KEY="replace-with-a-long-random-secret" --app your-app-name
heroku config:set SECRET_ENCRYPTION_KEY="replace-with-a-fernet-key" --app your-app-name
heroku config:set DJANGO_ALLOWED_HOSTS="your-app-name.herokuapp.com" --app your-app-name
heroku config:set ENABLE_DEMO_DATABASE_CONNECTION=true --app your-app-name
```

Generate a Fernet key for `SECRET_ENCRYPTION_KEY`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Deploy from GitHub by connecting the Heroku app to the repository and enabling
automatic deploys from `main`, or deploy from the CLI:

```powershell
git push heroku main
```

After the first deploy, create a bootstrap admin:

```powershell
heroku run python manage.py createsuperuser --app your-app-name
```

Then open `/admin/`, create your first `Organization`, and add a `Membership`
for that user as `Company admin`. The release process will have already run
migrations and will keep the bundled demo database connection available.
