from celery import Celery

from app.config import settings

celery_app = Celery(
    "helpdesk_assistant",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    worker_concurrency=2,
    worker_max_tasks_per_child=100,
    worker_prefetch_multiplier=1,  # Prevents idle workers from hoarding tasks
)

