"""Configuración base de la aplicación Celery."""

from app.core.config import settings
from app.workers.celery_app import celery_app


def test_celery_app_uses_configured_redis_broker():
    """Celery toma el broker de Settings y conserva una identidad estable."""

    assert celery_app.main == "ticketing"
    assert celery_app.conf.broker_url == settings.celery_broker_url


def test_celery_app_uses_safe_message_and_result_settings():
    """Las tareas usan JSON, UTC y no almacenan resultados innecesarios."""

    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.enable_utc is True
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.broker_connection_retry_on_startup is True
    assert celery_app.conf.result_backend is None
