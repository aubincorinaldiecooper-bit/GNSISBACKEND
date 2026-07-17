web: uvicorn gnsis.service.api:app --host 0.0.0.0 --port ${PORT:-8000}
worker: celery -A gnsis.service.tasks.celery_app worker --loglevel=info --concurrency=${GNSIS_WORKER_CONCURRENCY:-2}
release: gnsis-migrate
beat: celery -A gnsis.service.tasks.celery_app beat --loglevel=info --pidfile=/tmp/celerybeat.pid --schedule=/tmp/celerybeat-schedule
