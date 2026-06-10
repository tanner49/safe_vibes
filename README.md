# Safe Vibes

Safe Vibes is a Django app for building and sharing AI-generated HTML reports
without turning every sales ops or finance experiment into unmanaged shadow IT.

Users chat with an AI report builder, connect approved databases, generate SQL
and HTML, preview the result, and publish reports to the right people. Admins get
the governance layer: database connections, model controls, query limits, cache
settings, SSO configuration, IP allowlists, and external URL policies.

## Why This Exists

Business teams are already vibe-coding reports with AI. They paste SQL into
tools, pass around HTML files, and accidentally create cost, security, and
governance problems.

Safe Vibes gives those teams a safer place to build while giving engineering
and IT a control plane.

## Main Features

- Chat-style AI builder for HTML + SQL reports
- Governed database connections for Postgres, SQLite demo data, BigQuery, and Snowflake
- Read-only SQL policy checks, row/byte limits, query timeouts, and cached report data
- Published report sharing by owner, whole organization, or specific users
- Admin-managed AI providers, API keys, allowed models, and default models
- Organization settings for SSO, report policy, security policy, users, and databases
- IP allowlists for report access
- External report URL whitelist/blacklist rules with CSP and runtime guards
- Bundled fake SaaS sales demo warehouse for first-run testing
- Docker Compose deployment path, with optional Heroku notes

## Local Quickstart

```powershell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py ensure_demo_database
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

Open:

```text
http://127.0.0.1:8000/
```

Then open `/admin/` and create:

1. An `Organization`
2. A `Membership` connecting your user to the organization
3. Role: `Company admin`

The app uses email as the user identity. Django's internal `username` field is
filled from email automatically.

## Environment Variables

Copy `.env.example` to `.env` for local non-Docker development.

Important values:

```env
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=replace-me
SECRET_ENCRYPTION_KEY=replace-with-fernet-key
DJANGO_ALLOWED_HOSTS=your-domain.example.com
DATABASE_URL=postgres://user:password@host:5432/dbname
DATABASE_CONN_MAX_AGE=0
REPORT_CACHE_ENABLED=true
ENABLE_DEMO_DATABASE_CONNECTION=true
```

Generate `SECRET_ENCRYPTION_KEY`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`DATABASE_CONN_MAX_AGE=0` is intentional for the ASGI deployment. Django
persistent database connections are not recommended under ASGI; keeping them
disabled avoids intermittent stale Postgres connections.

## Docker Compose Deployment

Docker Compose is the recommended generic deployment entrypoint. The included
compose file runs the app plus Postgres and uses the same ASGI/Gunicorn/Uvicorn
server shape as production.

```powershell
docker compose up --build
```

Open:

```text
http://127.0.0.1:8000/
```

Create a superuser in the running container:

```powershell
docker compose exec web python manage.py createsuperuser
```

The default compose database is:

```env
DATABASE_URL=postgres://save_vibes:save_vibes@db:5432/save_vibes
```

For a real Docker deployment:

- Replace the `db` service with your managed Postgres, or keep it for a simple
  single-host deployment.
- Set `DATABASE_URL` to your managed database when you swap out the bundled
  Postgres service.
- Set a real `DJANGO_SECRET_KEY`.
- Set a real `SECRET_ENCRYPTION_KEY`.
- Set `DJANGO_ALLOWED_HOSTS` to your domain.
- Keep `DATABASE_CONN_MAX_AGE=0` unless you deliberately change away from ASGI.
- Keep `RUN_MIGRATIONS=true` for simple deployments, or run migrations as a
  separate release job if your platform supports one.

The Docker entrypoint runs:

```text
python manage.py migrate --noinput
python manage.py ensure_demo_database
```

Disable either with:

```env
RUN_MIGRATIONS=false
ENABLE_DEMO_DATABASE_CONNECTION=false
```

For platforms that deploy Docker images directly, use the same image built by
the `Dockerfile` and provide these environment variables through the platform's
secret/config system. The container listens on `PORT` and defaults to `8000`.

## Heroku Deployment

Heroku is optional. The app is prepared for it with:

- `Procfile` web process using Gunicorn + Uvicorn ASGI workers
- `Procfile` release process for migrations and demo database setup
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
heroku config:set DATABASE_CONN_MAX_AGE=0 --app your-app-name
heroku config:set DJANGO_LOG_LEVEL=INFO --app your-app-name
heroku config:set WEB_CONCURRENCY=2 --app your-app-name
heroku config:set WEB_TIMEOUT=180 --app your-app-name
```

Deploy from GitHub or the CLI:

```powershell
git push heroku main
```

Create a bootstrap admin:

```powershell
heroku run python manage.py createsuperuser --app your-app-name
```

Then create an organization and membership in `/admin/`.

## Demo Data

The repo includes a small read-only SQLite demo warehouse at:

```text
demo_data/demo_sales.sqlite3
```

New organizations automatically get a `Demo SaaS Sales` database connection when
`ENABLE_DEMO_DATABASE_CONNECTION=true`.

Refresh the committed demo file:

```powershell
python services/build_demo_sqlite.py
```

Backfill demo connections:

```powershell
python manage.py ensure_demo_database
```

## Database Connections

Supported report data sources:

- SQLite for bundled demo data
- Postgres via SQLAlchemy async drivers
- BigQuery via REST `jobs.query` with async polling
- Snowflake via the Snowflake SQL API

In Settings > Database connections, company admins can create approved
connections. Secrets are encrypted with `SECRET_ENCRYPTION_KEY` and only
redacted previews are displayed.

## AI Providers

In Settings > AI providers, admins can add provider keys and choose allowed
models. The builder uses the admin-selected default model unless a draft has a
valid explicit override.

Supported providers:

- OpenAI
- Claude / Anthropic
- Gemini via `google-genai`

## SSO

Settings > SSO provides a handholding OIDC configuration page:

- Issuer URL
- Client ID
- Client secret
- Scopes
- Sign-in URL
- Redirect / callback URL
- Require SSO toggle

Users who complete SSO through an organization's configured login URL are
created automatically if needed and added to that organization as viewers.
Company admins can promote them later. The `Require SSO` toggle blocks password
login for non-staff users in that organization. Staff and superusers can still
use password login for bootstrap/admin access.

## Security Model

Safe Vibes includes several guardrails:

- Read-only SQL policy checks
- Query timeout, row count, raw byte, and compressed cache limits
- Optional report data caching with TTL
- Encrypted AI, database, SSO, and warehouse credentials
- Organization-level report sharing controls
- IP allowlists for report access
- External report URL whitelist/blacklist rules
- CSP and runtime `fetch`/`XMLHttpRequest` guards in report previews

These are practical MVP controls, not a formal security proof. Review the code
and policies before exposing sensitive production data.

## Development Notes

Run tests:

```powershell
python manage.py test core.tests
python manage.py check
```

Run a disposable local Postgres demo warehouse:

```powershell
docker compose -f services/docker-compose.postgres.yml up -d
python services/seed_demo_postgres.py
```

## License

Safe Vibes is released under the Apache License 2.0. See [LICENSE](LICENSE).
