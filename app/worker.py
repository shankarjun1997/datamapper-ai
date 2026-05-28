"""
app/worker.py — Celery application for async pipeline execution.

The xREF DataMapper backend hands long-running mapping pipelines off to a
Celery worker pool so the FastAPI event loop is never blocked. Redis acts as
both broker and result backend (already provisioned in docker-compose).

Run a worker locally with:

    celery -A app.worker worker --loglevel=info --concurrency=2
"""
from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "xref_worker",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks.pipeline_task"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_time_limit=600,          # hard kill after 10 min
    task_soft_time_limit=540,     # soft kill at 9 min
)
