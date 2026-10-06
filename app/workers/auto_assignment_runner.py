"""Ejecutor compartido para procesar una tanda de autoasignaciones."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.ticket_auto_assignment_service import (
    AutoAssignmentResult,
    process_due_auto_assignments,
)


AUTO_ASSIGNMENT_LOCK_NAME = "ticketing:auto-assignment:lock"
logger = logging.getLogger(__name__)


class AutoAssignmentRunStatus(str, Enum):
    """Indica si la tanda se ejecutó o si ya había otra en curso."""

    COMPLETED = "COMPLETED"
    SKIPPED_LOCK_HELD = "SKIPPED_LOCK_HELD"


@dataclass(frozen=True)
class AutoAssignmentRunResult:
    """Resultado del runner junto con el resumen producido por el service."""

    status: AutoAssignmentRunStatus
    assignments: AutoAssignmentResult | None


def _create_redis_client():
    """Crea el cliente al ejecutar el runner, no al importar la aplicación."""

    from redis import Redis

    return Redis.from_url(settings.redis_url)


def run_auto_assignment_batch(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    redis_client_factory: Callable[[], Any] = _create_redis_client,
    processor: Callable[..., AutoAssignmentResult] = process_due_auto_assignments,
) -> AutoAssignmentRunResult:
    """Ejecuta una tanda solo si puede obtener el lock distribuido.

    El script manual y la futura tarea Celery deben entrar por esta función.
    Así ninguna de las dos puede procesar una tanda mientras la otra está activa.
    """

    redis_client = redis_client_factory()
    lock = None
    acquired = False
    db = None

    try:
        lock = redis_client.lock(
            AUTO_ASSIGNMENT_LOCK_NAME,
            timeout=settings.auto_assignment_lock_timeout_seconds,
        )
        # No espera el lock: Celery volverá a intentarlo en su próxima ejecución.
        acquired = lock.acquire(blocking=False)
        if not acquired:
            logger.info("Auto-assignment batch skipped because another run holds the lock")
            return AutoAssignmentRunResult(
                status=AutoAssignmentRunStatus.SKIPPED_LOCK_HELD,
                assignments=None,
            )

        db = session_factory()
        assignments = processor(
            db,
            limit=settings.auto_assignment_batch_size_per_phase,
        )
        return AutoAssignmentRunResult(
            status=AutoAssignmentRunStatus.COMPLETED,
            assignments=assignments,
        )
    finally:
        if db is not None:
            db.close()

        if acquired and lock is not None:
            try:
                lock.release()
            except Exception:
                # Si el lock venció o Redis cayó, no se oculta el resultado de
                # la tanda, pero el incidente queda registrado para monitoreo.
                logger.exception("Could not release auto-assignment Redis lock")

        redis_client.close()
