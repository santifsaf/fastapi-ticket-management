"""Procesa una tanda de autoasignaciones vencidas.

Uso:
    python -m app.scripts.process_auto_assignments

El script usa el mismo runner protegido por Redis que utilizará Celery. Por eso
no puede solaparse con otra ejecución manual o automática.
"""

from app.workers.auto_assignment_runner import (
    AutoAssignmentRunStatus,
    run_auto_assignment_batch,
)


def main() -> None:
    run_result = run_auto_assignment_batch()
    if run_result.status == AutoAssignmentRunStatus.SKIPPED_LOCK_HELD:
        print("Tanda omitida: ya existe otra ejecución de autoasignación en curso.")
        return

    result = run_result.assignments
    assert result is not None
    print(f"Teams asignados: {result.teams_assigned}")
    print(f"Responsables asignados: {result.users_assigned}")
    print(f"Sin team candidato: {result.teams_without_candidate}")
    print(f"Sin responsable candidato: {result.users_without_candidate}")
    print(f"Omitidos: {result.skipped}")
    print(f"Errores: {result.errors}")


if __name__ == "__main__":
    main()
