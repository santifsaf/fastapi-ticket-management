"""Flujos criticos de tickets ejecutados contra PostgreSQL real."""

from datetime import datetime, timezone
from threading import Event, Thread
from time import sleep
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.category import CategoryTeam, TeamAssignmentStrategy, TicketCategory
from app.models.team import AssignmentStrategy, Team, TeamMember
from app.models.ticket import (
    Ticket,
    AssignmentSource,
    TicketAssignmentHistory,
    TicketCommentVisibility,
    TicketDependency,
    TicketStatus,
    TicketStatusHistory,
    TicketTeamHistory,
)
from app.models.user import User, UserRole
from app.schemas.ticket import TicketCommentCreate, TicketCreate
from app.services.ticket_comment_service import create_ticket_comment, get_ticket_comments
from app.services.ticket_assignment_service import assign_ticket, claim_ticket
from app.services.ticket_auto_assignment_service import find_team_auto_assignment_candidate
from app.services.ticket_auto_assignment_service import process_due_auto_assignments
from app.services.ticket_dependency_service import add_ticket_dependency
from app.services.ticket_exceptions import (
    TicketBlockedByOpenDependenciesError,
    TicketPermissionError,
)
from app.services.ticket_lifecycle_service import change_ticket_status
from app.services.ticket_service import create_ticket_service, get_ticket_detail


# Marca todos los tests del archivo para poder incluirlos o excluirlos con -m.
pytestmark = pytest.mark.integration


def _create_user(db, role: UserRole) -> User:
    """Inserta un usuario minimo y deja disponible su UUID sin hacer commit."""

    user = User(
        # El UUID en el email evita colisiones entre ejecuciones o tests.
        email=f"{uuid4()}@example.com",
        password_hash="hash-de-test",
        role=role,
        is_active=True,
    )
    db.add(user)
    # flush ejecuta el INSERT dentro de la transaccion actual y asigna user.id.
    db.flush()
    return user


def _create_category(db) -> TicketCategory:
    """Inserta una categoria activa requerida por los tickets de prueba."""

    category = TicketCategory(name=f"Categoria {uuid4()}", is_active=True)
    db.add(category)
    db.flush()
    return category


def _create_ticket(db, creator: User, category: TicketCategory, **overrides) -> Ticket:
    """Crea un ticket base y permite reemplazar campos para cada escenario."""

    values = {
        "title": "Ticket de integracion",
        "description": "Comprueba persistencia real",
        "status": TicketStatus.OPEN,
        "created_by": creator.id,
        "category_id": category.id,
    }
    # Ejemplo: assigned_to=agent.id reemplaza solo ese campo del escenario base.
    values.update(overrides)
    ticket = Ticket(**values)
    db.add(ticket)
    db.flush()
    return ticket


def test_create_ticket_persists_server_managed_fields(integration_db):
    """El service completa estado, creador y fecha usando PostgreSQL real."""

    # Preparacion: entidades que el caso de uso necesita por foreign key.
    user = _create_user(integration_db, UserRole.USER)
    category = _create_category(integration_db)

    # Ejecucion: se usa TicketCreate y el service real, incluido su commit().
    ticket = create_ticket_service(
        integration_db,
        TicketCreate(
            title="Error de login",
            description="No puedo ingresar",
            category_id=category.id,
        ),
        user,
    )

    # Verificacion: el objeto confirmado conserva los valores del servidor.
    persisted_ticket = integration_db.get(Ticket, ticket.id)
    assert persisted_ticket is not None
    assert persisted_ticket.status == TicketStatus.OPEN
    assert persisted_ticket.created_by == user.id
    assert persisted_ticket.category_id == category.id
    assert persisted_ticket.created_at is not None


def test_team_member_can_read_ticket_detail(integration_db):
    """Comprueba la consulta real de membresia usada por el service."""

    creator = _create_user(integration_db, UserRole.USER)
    agent = _create_user(integration_db, UserRole.AGENT)
    category = _create_category(integration_db)
    team = Team(name=f"Team {uuid4()}")
    integration_db.add(team)
    integration_db.flush()
    integration_db.add(TeamMember(team_id=team.id, user_id=agent.id))
    integration_db.flush()
    ticket = _create_ticket(integration_db, creator, category, team_id=team.id)

    result = get_ticket_detail(integration_db, ticket.id, agent)

    assert result.id == ticket.id


def test_unrelated_user_cannot_read_ticket_detail(integration_db):
    creator = _create_user(integration_db, UserRole.USER)
    unrelated_user = _create_user(integration_db, UserRole.USER)
    category = _create_category(integration_db)
    ticket = _create_ticket(integration_db, creator, category)

    with pytest.raises(TicketPermissionError, match="Not enough permissions to view ticket"):
        get_ticket_detail(integration_db, ticket.id, unrelated_user)


def test_status_change_persists_ticket_and_history_atomically(integration_db):
    """Un cambio valido actualiza el ticket y genera su evento de auditoria."""

    # El AGENT debe estar asignado directamente para poder cambiar el estado.
    agent = _create_user(integration_db, UserRole.AGENT)
    category = _create_category(integration_db)
    team = Team(name=f"Team {uuid4()}")
    integration_db.add(team)
    integration_db.flush()
    integration_db.add(TeamMember(team_id=team.id, user_id=agent.id))
    integration_db.flush()
    ticket = _create_ticket(
        integration_db,
        agent,
        category,
        team_id=team.id,
        assigned_to=agent.id,
    )

    result = change_ticket_status(
        integration_db,
        ticket.id,
        TicketStatus.IN_PROGRESS,
        agent,
    )

    # Esta consulta real comprueba que el service inserto la fila de historial.
    history = (
        integration_db.query(TicketStatusHistory)
        .filter(TicketStatusHistory.ticket_id == ticket.id)
        .one()
    )
    assert result.status == TicketStatus.IN_PROGRESS
    assert history.old_status == TicketStatus.OPEN
    assert history.new_status == TicketStatus.IN_PROGRESS
    assert history.changed_by == agent.id
    assert history.changed_at is not None


def test_open_dependency_prevents_resolving_ticket(integration_db):
    """Una dependencia activa impide resolver el ticket que esta bloqueado."""

    admin = _create_user(integration_db, UserRole.ADMIN)
    category = _create_category(integration_db)
    blocked_ticket = _create_ticket(
        integration_db,
        admin,
        category,
        status=TicketStatus.IN_PROGRESS,
    )
    blocking_ticket = _create_ticket(integration_db, admin, category)

    # blocked_ticket no puede resolverse mientras blocking_ticket siga abierto.
    dependency = add_ticket_dependency(
        integration_db,
        blocked_ticket.id,
        blocking_ticket.id,
        admin,
        reason="Falta resolver el ticket bloqueante",
    )

    # Esperamos una excepcion de dominio, no una respuesta HTTP del router.
    with pytest.raises(TicketBlockedByOpenDependenciesError):
        change_ticket_status(
            integration_db,
            blocked_ticket.id,
            TicketStatus.RESOLVED,
            admin,
        )

    # El intento fallido no elimina la dependencia ni cambia el estado actual.
    persisted_dependency = integration_db.get(TicketDependency, dependency.id)
    assert persisted_dependency is not None
    assert persisted_dependency.is_active is True
    assert blocked_ticket.status == TicketStatus.IN_PROGRESS


def test_requester_receives_only_public_comments(integration_db):
    """El filtro de visibilidad se comprueba con consultas SQL reales."""

    requester = _create_user(integration_db, UserRole.USER)
    admin = _create_user(integration_db, UserRole.ADMIN)
    category = _create_category(integration_db)
    ticket = _create_ticket(integration_db, requester, category)

    public_comment = create_ticket_comment(
        integration_db,
        ticket.id,
        TicketCommentCreate(
            body="Respuesta visible para el solicitante",
            visibility=TicketCommentVisibility.REQUESTER_VISIBLE,
        ),
        requester,
    )
    create_ticket_comment(
        integration_db,
        ticket.id,
        TicketCommentCreate(
            body="Nota operativa que el solicitante no debe ver",
            visibility=TicketCommentVisibility.INTERNAL,
        ),
        admin,
    )

    requester_comments = get_ticket_comments(integration_db, ticket.id, requester)
    admin_comments = get_ticket_comments(integration_db, ticket.id, admin)

    assert [comment.id for comment in requester_comments] == [public_comment.id]
    assert len(admin_comments) == 2


@pytest.mark.parametrize("operational_role", [UserRole.AGENT, UserRole.ADMIN])
def test_operational_member_claims_team_ticket_and_persists_assignment_history(
    integration_db,
    operational_role,
):
    """Comprueba el reclamo de agentes y admins miembros con persistencia real."""

    requester = _create_user(integration_db, UserRole.USER)
    operational_user = _create_user(integration_db, operational_role)
    category = _create_category(integration_db)
    team = Team(name=f"Team {uuid4()}", self_assignment_enabled=True)
    integration_db.add(team)
    integration_db.flush()
    integration_db.add(TeamMember(team_id=team.id, user_id=operational_user.id))
    integration_db.flush()
    ticket = _create_ticket(integration_db, requester, category, team_id=team.id)

    result = claim_ticket(integration_db, ticket.id, operational_user)

    history = (
        integration_db.query(TicketAssignmentHistory)
        .filter(TicketAssignmentHistory.ticket_id == ticket.id)
        .one()
    )
    assert result.assigned_to == operational_user.id
    assert history.old_assigned_to is None
    assert history.new_assigned_to == operational_user.id
    assert history.changed_by == operational_user.id
    integration_db.refresh(operational_user)
    assert operational_user.last_assigned_at is not None


def test_manual_assignment_waits_for_lock_and_uses_latest_ticket_state(integration_engine):
    """Una asignacion manual espera el lock y no sobrescribe datos antiguos."""

    setup_db = Session(integration_engine)
    lock_db = Session(integration_engine)
    worker = None
    worker_started = Event()
    worker_finished = Event()
    worker_errors = []

    try:
        requester = _create_user(setup_db, UserRole.USER)
        automatic_agent = _create_user(setup_db, UserRole.AGENT)
        manual_agent = _create_user(setup_db, UserRole.AGENT)
        admin = _create_user(setup_db, UserRole.ADMIN)
        category = _create_category(setup_db)
        team = Team(name=f"Team concurrente {uuid4()}")
        setup_db.add(team)
        setup_db.flush()
        setup_db.add_all(
            [
                TeamMember(team_id=team.id, user_id=automatic_agent.id),
                TeamMember(team_id=team.id, user_id=manual_agent.id),
            ]
        )
        ticket = _create_ticket(setup_db, requester, category, team_id=team.id)
        setup_db.commit()

        ticket_id = ticket.id
        team_id = team.id
        category_id = category.id
        requester_id = requester.id
        automatic_agent_id = automatic_agent.id
        manual_agent_id = manual_agent.id
        admin_id = admin.id

        # Simula otra operacion que ya tiene la fila y prepara una asignacion.
        locked_ticket = (
            lock_db.query(Ticket)
            .filter(Ticket.id == ticket_id)
            .with_for_update()
            .one()
        )
        locked_ticket.assigned_to = automatic_agent_id
        lock_db.flush()

        def run_manual_assignment():
            worker_db = Session(integration_engine)
            try:
                worker_started.set()
                current_admin = worker_db.get(User, admin_id)
                assign_ticket(worker_db, ticket_id, manual_agent_id, current_admin)
            except Exception as exc:  # El hilo comunica el error al test principal.
                worker_errors.append(exc)
            finally:
                worker_db.close()
                worker_finished.set()

        worker = Thread(target=run_manual_assignment)
        worker.start()
        assert worker_started.wait(timeout=2)

        # Mientras la primera transaccion conserva FOR UPDATE, la segunda espera.
        sleep(0.2)
        assert worker_finished.is_set() is False

        lock_db.commit()
        worker.join(timeout=5)
        assert worker_finished.is_set() is True
        assert worker_errors == []

        verification_db = Session(integration_engine)
        try:
            persisted_ticket = verification_db.get(Ticket, ticket_id)
            history = (
                verification_db.query(TicketAssignmentHistory)
                .filter(TicketAssignmentHistory.ticket_id == ticket_id)
                .one()
            )

            assert persisted_ticket.assigned_to == manual_agent_id
            # El historial prueba que el service releyo el valor confirmado al liberar el lock.
            assert history.old_assigned_to == automatic_agent_id
            assert history.new_assigned_to == manual_agent_id
            assert history.changed_by == admin_id
        finally:
            verification_db.close()
    finally:
        # Libera cualquier lock pendiente antes de esperar o limpiar el escenario.
        lock_db.rollback()
        lock_db.close()
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        setup_db.close()

        if "ticket_id" in locals():
            cleanup_db = Session(integration_engine)
            try:
                cleanup_db.query(TicketAssignmentHistory).filter(
                    TicketAssignmentHistory.ticket_id == ticket_id
                ).delete(synchronize_session=False)
                cleanup_db.query(Ticket).filter(Ticket.id == ticket_id).delete(
                    synchronize_session=False
                )
                cleanup_db.query(TeamMember).filter(TeamMember.team_id == team_id).delete(
                    synchronize_session=False
                )
                cleanup_db.query(Team).filter(Team.id == team_id).delete(
                    synchronize_session=False
                )
                cleanup_db.query(User).filter(
                    User.id.in_([requester_id, automatic_agent_id, manual_agent_id, admin_id])
                ).delete(synchronize_session=False)
                cleanup_db.query(TicketCategory).filter(
                    TicketCategory.id == category_id
                ).delete(synchronize_session=False)
                cleanup_db.commit()
            except Exception:
                cleanup_db.rollback()
                raise
            finally:
                cleanup_db.close()


def test_least_active_selector_uses_real_team_members_and_ticket_counts(integration_db):
    """Comprueba candidatos, roles y carga activa mediante consultas SQL reales."""

    requester = _create_user(integration_db, UserRole.USER)
    busy_agent = _create_user(integration_db, UserRole.AGENT)
    available_admin = _create_user(integration_db, UserRole.ADMIN)
    inactive_agent = _create_user(integration_db, UserRole.AGENT)
    inactive_agent.is_active = False
    category = _create_category(integration_db)
    team = Team(name=f"Team {uuid4()}")
    integration_db.add(team)
    integration_db.flush()
    integration_db.add_all(
        [
            # El selector también se protege ante una membresía inválida creada fuera del service.
            TeamMember(team_id=team.id, user_id=requester.id),
            TeamMember(team_id=team.id, user_id=busy_agent.id),
            TeamMember(team_id=team.id, user_id=available_admin.id),
            TeamMember(team_id=team.id, user_id=inactive_agent.id),
        ]
    )
    _create_ticket(
        integration_db,
        requester,
        category,
        team_id=team.id,
        assigned_to=busy_agent.id,
        status=TicketStatus.IN_PROGRESS,
    )
    integration_db.flush()

    result = find_team_auto_assignment_candidate(
        integration_db,
        team.id,
        AssignmentStrategy.LEAST_ACTIVE,
    )

    assert result.id == available_admin.id


def test_due_ticket_is_assigned_to_team_and_member_in_one_processing_run(integration_db):
    """Comprueba routing, responsable, vencimientos e historiales automáticos."""

    requester = _create_user(integration_db, UserRole.USER)
    team_a_busy_agent = _create_user(integration_db, UserRole.AGENT)
    team_a_available_admin = _create_user(integration_db, UserRole.ADMIN)
    team_b_agent = _create_user(integration_db, UserRole.AGENT)
    category = TicketCategory(
        name=f"Categoria {uuid4()}",
        is_active=True,
        auto_team_assignment_enabled=True,
        team_assignment_delay_minutes=0,
        team_assignment_strategy=TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )
    team_a = Team(
        name=f"Team A {uuid4()}",
        auto_assignment_enabled=True,
        auto_assignment_delay_minutes=0,
        assignment_strategy=AssignmentStrategy.LEAST_ACTIVE,
    )
    team_b = Team(
        name=f"Team B {uuid4()}",
        auto_assignment_enabled=True,
        auto_assignment_delay_minutes=0,
        assignment_strategy=AssignmentStrategy.LEAST_ACTIVE,
    )
    integration_db.add_all([category, team_a, team_b])
    integration_db.flush()
    integration_db.add_all(
        [
            CategoryTeam(category_id=category.id, team_id=team_a.id),
            CategoryTeam(category_id=category.id, team_id=team_b.id),
            TeamMember(team_id=team_a.id, user_id=team_a_busy_agent.id),
            TeamMember(team_id=team_a.id, user_id=team_a_available_admin.id),
            TeamMember(team_id=team_b.id, user_id=team_b_agent.id),
        ]
    )
    _create_ticket(
        integration_db,
        requester,
        category,
        team_id=team_a.id,
        assigned_to=team_a_busy_agent.id,
        status=TicketStatus.IN_PROGRESS,
    )
    _create_ticket(
        integration_db,
        requester,
        category,
        team_id=team_b.id,
        assigned_to=team_b_agent.id,
        status=TicketStatus.IN_PROGRESS,
    )
    integration_db.commit()

    ticket = create_ticket_service(
        integration_db,
        TicketCreate(
            title="Ticket para routing automático",
            description="Debe atravesar ambas etapas",
            category_id=category.id,
        ),
        requester,
    )
    result = process_due_auto_assignments(
        integration_db,
        now=datetime.now(timezone.utc),
    )
    integration_db.refresh(ticket)

    assert result.teams_assigned == 1
    assert result.users_assigned == 1
    assert result.teams_without_candidate == 0
    assert result.users_without_candidate == 0
    assert result.skipped == 0
    assert result.errors == 0
    assert ticket.team_id == team_a.id
    assert ticket.assigned_to == team_a_available_admin.id
    assert ticket.team_queue_entered_at is None
    assert ticket.team_assignment_due_at is None
    assert ticket.auto_assignment_due_at is None

    team_history = (
        integration_db.query(TicketTeamHistory)
        .filter(TicketTeamHistory.ticket_id == ticket.id)
        .one()
    )
    assignment_history = (
        integration_db.query(TicketAssignmentHistory)
        .filter(TicketAssignmentHistory.ticket_id == ticket.id)
        .one()
    )
    assert team_history.changed_by is None
    assert team_history.source == AssignmentSource.AUTOMATIC
    assert assignment_history.changed_by is None
    assert assignment_history.source == AssignmentSource.AUTOMATIC


def test_due_ticket_remains_queued_when_category_has_no_operational_team(integration_db):
    """Un intento sin candidatos no consume el vencimiento ni altera el ticket."""

    requester = _create_user(integration_db, UserRole.USER)
    category = TicketCategory(
        name=f"Categoria sin candidatos {uuid4()}",
        is_active=True,
        auto_team_assignment_enabled=True,
        team_assignment_delay_minutes=0,
        team_assignment_strategy=TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )
    integration_db.add(category)
    integration_db.commit()
    ticket = create_ticket_service(
        integration_db,
        TicketCreate(
            title="Ticket que debe seguir en cola",
            description="No hay equipos operativos disponibles",
            category_id=category.id,
        ),
        requester,
    )
    original_due_at = ticket.team_assignment_due_at

    result = process_due_auto_assignments(
        integration_db,
        now=datetime.now(timezone.utc),
    )
    integration_db.refresh(ticket)

    assert result.teams_assigned == 0
    assert result.users_assigned == 0
    assert result.teams_without_candidate == 1
    assert result.users_without_candidate == 0
    assert result.skipped == 0
    assert result.errors == 0
    assert ticket.team_id is None
    assert ticket.assigned_to is None
    assert ticket.team_assignment_due_at == original_due_at
    assert (
        integration_db.query(TicketTeamHistory)
        .filter(TicketTeamHistory.ticket_id == ticket.id)
        .count()
        == 0
    )
