"""Pruebas de la adaptación entre Celery y el runner de autoasignación."""

from app.services.ticket_auto_assignment_service import AutoAssignmentResult
from app.workers.auto_assignment_runner import (
    AutoAssignmentRunResult,
    AutoAssignmentRunStatus,
)
from app.workers import tasks


def test_task_returns_completed_batch_summary(monkeypatch):
    """La tarea transforma el resultado del runner en datos serializables."""

    run_result = AutoAssignmentRunResult(
        status=AutoAssignmentRunStatus.COMPLETED,
        assignments=AutoAssignmentResult(
            teams_assigned=2,
            users_assigned=3,
            teams_without_candidate=1,
            users_without_candidate=0,
            skipped=4,
            errors=0,
        ),
    )

    def fake_run_auto_assignment_batch():
        return run_result

    monkeypatch.setattr(
        tasks,
        "run_auto_assignment_batch",
        fake_run_auto_assignment_batch,
    )

    result = tasks.process_auto_assignments_task.run()

    assert result == {
        "status": "COMPLETED",
        "assignments": {
            "teams_assigned": 2,
            "users_assigned": 3,
            "teams_without_candidate": 1,
            "users_without_candidate": 0,
            "skipped": 4,
            "errors": 0,
        },
    }


def test_task_reports_when_distributed_lock_is_held(monkeypatch):
    """Una ejecución solapada se informa como omitida y no produce resumen."""

    run_result = AutoAssignmentRunResult(
        status=AutoAssignmentRunStatus.SKIPPED_LOCK_HELD,
        assignments=None,
    )

    def fake_run_auto_assignment_batch():
        return run_result

    monkeypatch.setattr(
        tasks,
        "run_auto_assignment_batch",
        fake_run_auto_assignment_batch,
    )

    result = tasks.process_auto_assignments_task.run()

    assert result == {"status": "SKIPPED_LOCK_HELD"}
