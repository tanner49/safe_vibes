release: python manage.py migrate && python manage.py ensure_demo_database
web: gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} --timeout ${WEB_TIMEOUT:-180}
