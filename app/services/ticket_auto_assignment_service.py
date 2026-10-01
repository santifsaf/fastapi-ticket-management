"""Selección y ejecución de la autoasignación por categoría y equipo."""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from fractions import Fraction
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.category import CategoryTeam, TeamAssignmentStrategy
from app.models.team import AssignmentStrategy, Team, TeamMember
from app.models.ticket import AssignmentSource, Ticket, TicketStatus, TicketTeamHistory
from app.models.user import User, UserRole
from app.services.assignment_timing import calculate_assignment_due_at
from app.services.ticket_assignment_service import complete_ticket_user_assignment
from app.services.ticket_service_utils import commit_and_refresh


# RESOLVED y CLOSED ya no representan trabajo operativo pendiente.
ACTIVE_TICKET_STATUSES = {
    TicketStatus.OPEN,
    TicketStatus.IN_PROGRESS,
    TicketStatus.ON_HOLD,
}
OPERATIONAL_USER_ROLES = {UserRole.AGENT, UserRole.ADMIN}
logger = logging.getLogger(__name__)


class AssignmentOutcome(str, Enum):
    """Resultado esperado al intentar una autoasignación individual."""

    ASSIGNED = "ASSIGNED"
    NO_CANDIDATE = "NO_CANDIDATE"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class AutoAssignmentCandidate:
    """Usuario elegible acompañado por su cantidad de tickets activos."""

    user: User
    active_ticket_count: int


@dataclass(frozen=True)
class TeamAssignmentCandidate:
    """Equipo elegible acompañado por su carga y capacidad operativa."""

    team: Team
    active_ticket_count: int
    active_member_count: int

    @property
    def load_per_member(self) -> Fraction:
        return Fraction(self.active_ticket_count, self.active_member_count)


def _last_assignment_timestamp(last_assigned_at: datetime | None) -> float:
    """Convierte la fecha en un valor comparable y prioriza a quien nunca recibió tickets."""

    if last_assigned_at is None:
        return float("-inf")
    if last_assigned_at.tzinfo is None:
        last_assigned_at = last_assigned_at.replace(tzinfo=timezone.utc)
    return last_assigned_at.timestamp()


def choose_auto_assignment_candidate(
    candidates: list[AutoAssignmentCandidate],
    strategy: AssignmentStrategy,
) -> User | None:
    """Elige un candidato de forma determinista según la estrategia del team.

    Los desempates favorecen a quien lleva más tiempo sin asignaciones. El UUID
    aporta un último orden estable cuando todos los demás datos son iguales.
    """

    if not candidates:
        return None

    def longest_idle_key(candidate: AutoAssignmentCandidate) -> tuple[float, int, str]:
        return (
            _last_assignment_timestamp(candidate.user.last_assigned_at),
            candidate.active_ticket_count,
            str(candidate.user.id),
        )

    def least_active_key(candidate: AutoAssignmentCandidate) -> tuple[int, float, str]:
        return (
            candidate.active_ticket_count,
            _last_assignment_timestamp(candidate.user.last_assigned_at),
            str(candidate.user.id),
        )

    if strategy == AssignmentStrategy.LEAST_ACTIVE:
        selected = min(candidates, key=least_active_key)
    elif strategy == AssignmentStrategy.LONGEST_IDLE:
        selected = min(candidates, key=longest_idle_key)
    else:
        raise ValueError(f"Unsupported assignment strategy: {strategy}")

    return selected.user


def find_team_auto_assignment_candidate(
    db: Session,
    team_id: UUID,
    strategy: AssignmentStrategy,
) -> User | None:
    """Busca miembros operativos activos y aplica la estrategia configurada.

    La carga se cuenta en todos los equipos para no sobreasignar a una persona
    que participa simultáneamente en más de uno.
    """

    active_ticket_count = (
        select(func.count(Ticket.id))
        .where(
            Ticket.assigned_to == User.id,
            Ticket.status.in_(ACTIVE_TICKET_STATUSES),
            Ticket.archived_at.is_(None),
        )
        .correlate(User)
        .scalar_subquery()
    )

    rows = (
        db.query(User, active_ticket_count.label("active_ticket_count"))
        .join(TeamMember, TeamMember.user_id == User.id)
        .filter(
            TeamMember.team_id == team_id,
            User.is_active.is_(True),
            User.role.in_(OPERATIONAL_USER_ROLES),
        )
        .all()
    )
    candidates = [
        AutoAssignmentCandidate(user=user, active_ticket_count=int(ticket_count or 0))
        for user, ticket_count in rows
    ]
    return choose_auto_assignment_candidate(candidates, strategy)


def choose_team_assignment_candidate(
    candidates: list[TeamAssignmentCandidate],
    strategy: TeamAssignmentStrategy,
) -> Team | None:
    """Elige el equipo con menor carga proporcional por miembro activo."""

    if not candidates:
        return None

    def least_load_key(candidate: TeamAssignmentCandidate) -> tuple[Fraction, int, str]:
        return (
            candidate.load_per_member,
            candidate.active_ticket_count,
            str(candidate.team.id),
        )

    if strategy != TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER:
        raise ValueError(f"Unsupported team-assignment strategy: {strategy}")
    return min(candidates, key=least_load_key).team


def find_category_team_assignment_candidate(
    db: Session,
    category_id: UUID,
    strategy: TeamAssignmentStrategy,
) -> Team | None:
    """Obtiene equipos asociados con miembros activos y aplica el routing."""

    active_ticket_count = (
        select(func.count(Ticket.id))
        .where(
            Ticket.team_id == Team.id,
            Ticket.status.in_(ACTIVE_TICKET_STATUSES),
            Ticket.archived_at.is_(None),
        )
        .correlate(Team)
        .scalar_subquery()
    )
    active_member_count = (
        select(func.count(TeamMember.id))
        .join(User, User.id == TeamMember.user_id)
        .where(
            TeamMember.team_id == Team.id,
            User.is_active.is_(True),
            User.role.in_(OPERATIONAL_USER_ROLES),
        )
        .correlate(Team)
        .scalar_subquery()
    )

    rows = (
        db.query(
            Team,
            active_ticket_count.label("active_ticket_count"),
            active_member_count.label("active_member_count"),
        )
        .join(CategoryTeam, CategoryTeam.team_id == Team.id)
        .filter(
            CategoryTeam.category_id == category_id,
            active_member_count > 0,
        )
        .all()
    )
    candidates = [
        TeamAssignmentCandidate(
            team=team,
            active_ticket_count=int(ticket_count or 0),
            active_member_count=int(member_count),
        )
        for team, ticket_count, member_count in rows
    ]
    return choose_team_assignment_candidate(candidates, strategy)


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def auto_assign_ticket_to_team(
    db: Session,
    ticket_id: UUID,
    *,
    now: datetime | None = None,
) -> AssignmentOutcome:
    """Asigna un ticket vencido a un equipo y registra el evento automático."""

    current_time = now or datetime.now(timezone.utc)
    try:
        ticket = (
            db.query(Ticket)
            .filter(Ticket.id == ticket_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if ticket is None:
            # SKIP LOCKED también devuelve None si otro worker ya tomó la fila.
            db.rollback()
            return AssignmentOutcome.SKIPPED

        is_eligible = (
            ticket.team_id is None
            and ticket.assigned_to is None
            and ticket.status == TicketStatus.OPEN
            and ticket.archived_at is None
            and ticket.team_assignment_due_at is not None
            and ticket.team_assignment_strategy is not None
            and _utc_aware(ticket.team_assignment_due_at) <= _utc_aware(current_time)
        )
        if not is_eligible:
            db.rollback()
            return AssignmentOutcome.SKIPPED

        team = find_category_team_assignment_candidate(
            db,
            ticket.category_id,
            ticket.team_assignment_strategy,
        )
        if team is None:
            db.rollback()
            return AssignmentOutcome.NO_CANDIDATE

        ticket.team_id = team.id
        ticket.team_queue_entered_at = None
        ticket.team_assignment_due_at = None
        ticket.team_assignment_strategy = None
        ticket.auto_assignment_due_at = calculate_assignment_due_at(
            team.auto_assignment_enabled,
            team.auto_assignment_delay_minutes,
            now=current_time,
        )
        ticket.auto_assignment_strategy = (
            team.assignment_strategy if team.auto_assignment_enabled else None
        )
        db.add(
            TicketTeamHistory(
                ticket_id=ticket.id,
                old_team_id=None,
                new_team_id=team.id,
                changed_by=None,
                source=AssignmentSource.AUTOMATIC,
            )
        )
        commit_and_refresh(db, ticket)
        return AssignmentOutcome.ASSIGNED
    except Exception:
        db.rollback()
        raise


def auto_assign_ticket_to_user(
    db: Session,
    ticket_id: UUID,
    *,
    now: datetime | None = None,
) -> AssignmentOutcome:
    """Asigna un ticket vencido a un miembro y registra el evento automático."""

    current_time = now or datetime.now(timezone.utc)
    try:
        ticket = (
            db.query(Ticket)
            .filter(Ticket.id == ticket_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if ticket is None:
            # El ticket puede existir pero estar bloqueado por otro worker.
            db.rollback()
            return AssignmentOutcome.SKIPPED

        is_eligible = (
            ticket.team_id is not None
            and ticket.assigned_to is None
            and ticket.status == TicketStatus.OPEN
            and ticket.archived_at is None
            and ticket.auto_assignment_due_at is not None
            and ticket.auto_assignment_strategy is not None
            and _utc_aware(ticket.auto_assignment_due_at) <= _utc_aware(current_time)
        )
        if not is_eligible:
            db.rollback()
            return AssignmentOutcome.SKIPPED

        selected_user = find_team_auto_assignment_candidate(
            db,
            ticket.team_id,
            ticket.auto_assignment_strategy,
        )
        if selected_user is None:
            db.rollback()
            return AssignmentOutcome.NO_CANDIDATE

        complete_ticket_user_assignment(
            db,
            ticket,
            selected_user,
            changed_by_id=None,
            source=AssignmentSource.AUTOMATIC,
        )
        return AssignmentOutcome.ASSIGNED
    except Exception:
        db.rollback()
        raise


@dataclass(frozen=True)
class AutoAssignmentResult:
    """Resumen observable de una tanda de autoasignación."""

    teams_assigned: int
    users_assigned: int
    teams_without_candidate: int
    users_without_candidate: int
    skipped: int
    errors: int


def _find_due_team_ticket_ids(db: Session, current_time: datetime, limit: int) -> list[UUID]:
    """Obtiene la tanda vencida que todavía espera un team."""

    return [
        row[0]
        for row in (
            db.query(Ticket.id)
            .filter(
                Ticket.team_id.is_(None),
                Ticket.assigned_to.is_(None),
                Ticket.status == TicketStatus.OPEN,
                Ticket.archived_at.is_(None),
                Ticket.team_assignment_due_at.is_not(None),
                Ticket.team_assignment_due_at <= current_time,
            )
            .order_by(Ticket.team_assignment_due_at.asc())
            .limit(limit)
            .all()
        )
    ]


def _find_due_user_ticket_ids(db: Session, current_time: datetime, limit: int) -> list[UUID]:
    """Obtiene la tanda vencida que todavía espera un responsable."""

    return [
        row[0]
        for row in (
            db.query(Ticket.id)
            .filter(
                Ticket.team_id.is_not(None),
                Ticket.assigned_to.is_(None),
                Ticket.status == TicketStatus.OPEN,
                Ticket.archived_at.is_(None),
                Ticket.auto_assignment_due_at.is_not(None),
                Ticket.auto_assignment_due_at <= current_time,
            )
            .order_by(Ticket.auto_assignment_due_at.asc())
            .limit(limit)
            .all()
        )
    ]


def _process_ticket_ids(
    db: Session,
    ticket_ids: list[UUID],
    executor: Callable[..., AssignmentOutcome],
    *,
    current_time: datetime,
    phase: str,
) -> tuple[Counter, int]:
    """Aísla cada ticket para que un error no interrumpa toda la tanda."""

    outcomes: Counter = Counter()
    errors = 0
    for ticket_id in ticket_ids:
        try:
            outcome = executor(db, ticket_id, now=current_time)
            outcomes[outcome] += 1
        except Exception:
            # También recupera la sesión si el executor falló antes de su rollback.
            db.rollback()
            errors += 1
            logger.exception(
                "Error processing automatic ticket assignment",
                extra={
                    "ticket_id": str(ticket_id),
                    "assignment_phase": phase,
                },
            )
    return outcomes, errors


def process_due_auto_assignments(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> AutoAssignmentResult:
    """Procesa tickets vencidos; los que no tienen candidato quedan para reintento."""

    current_time = now or datetime.now(timezone.utc)
    team_outcomes, team_errors = _process_ticket_ids(
        db,
        _find_due_team_ticket_ids(db, current_time, limit),
        auto_assign_ticket_to_team,
        current_time=current_time,
        phase="TEAM",
    )

    user_outcomes, user_errors = _process_ticket_ids(
        db,
        _find_due_user_ticket_ids(db, current_time, limit),
        auto_assign_ticket_to_user,
        current_time=current_time,
        phase="USER",
    )
    return AutoAssignmentResult(
        teams_assigned=team_outcomes[AssignmentOutcome.ASSIGNED],
        users_assigned=user_outcomes[AssignmentOutcome.ASSIGNED],
        teams_without_candidate=team_outcomes[AssignmentOutcome.NO_CANDIDATE],
        users_without_candidate=user_outcomes[AssignmentOutcome.NO_CANDIDATE],
        skipped=(
            team_outcomes[AssignmentOutcome.SKIPPED]
            + user_outcomes[AssignmentOutcome.SKIPPED]
        ),
        errors=team_errors + user_errors,
    )
