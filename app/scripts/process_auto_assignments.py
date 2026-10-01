"""Procesa una tanda de autoasignaciones vencidas.

Uso:
    python -m app.scripts.process_auto_assignments

El script es un punto de entrada temporal y reutiliza el mismo service que más
adelante podrá invocar un worker periódico.
"""

from app.db.session import SessionLocal
from app.services.ticket_auto_assignment_service import process_due_auto_assignments


def main() -> None:
    db = SessionLocal()
    try:
        result = process_due_auto_assignments(db)
        print(f"Teams asignados: {result.teams_assigned}")
        print(f"Responsables asignados: {result.users_assigned}")
        print(f"Sin team candidato: {result.teams_without_candidate}")
        print(f"Sin responsable candidato: {result.users_without_candidate}")
        print(f"Omitidos: {result.skipped}")
        print(f"Errores: {result.errors}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
