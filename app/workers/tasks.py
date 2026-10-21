"""Tareas que los workers Celery pueden recibir desde Redis."""

import logging
from dataclasses import asdict

from app.workers.auto_assignment_runner import run_auto_assignment_batch
from app.workers.celery_app import celery_app


logger = logging.getLogger(__name__)


@celery_app.task(name="tickets.process_auto_assignments")
def process_auto_assignments_task() -> dict[str, object]:
    """Procesa una tanda protegida por el lock distribuido de Redis.

    La tarea se limita a adaptar Celery al runner compartido. La apertura de la
    sesión, el lock y la lógica de autoasignación permanecen fuera de Celery.
    """

    run_result = run_auto_assignment_batch()
    task_result = {"status": run_result.status.value}

    if run_result.assignments is not None:
        task_result["assignments"] = asdict(run_result.assignments)

    # Aunque no guardamos resultados en Redis, este resumen queda en el log.
    logger.info("Auto-assignment task finished: %s", task_result)
    return task_result
