release: python manage.py migrate && python manage.py ensure_demo_database
web: gunicorn config.wsgi:application
