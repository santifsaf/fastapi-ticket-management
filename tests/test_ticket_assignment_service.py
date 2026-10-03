"""Tests unitarios de asignacion, equipos y categorias de tickets."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.category import TeamAssignmentStrategy
from app.models.team import AssignmentStrategy
from app.models.ticket import (
    AssignmentSource,
    TicketAssignmentHistory,
    TicketCategoryHistory,
    TicketStatus,
    TicketTeamHistory,
)
from app.services.ticket_assignment_service import (
    assign_ticket,
    assign_ticket_to_team,
    change_ticket_category,
    claim_ticket,
)
from app.services.ticket_exceptions import (
    InvalidAssignedUserError,
    InvalidTicketCategoryChangeError,
    InvalidTicketCategoryError,
    MissingCategoryChangeReasonError,
    TicketAlreadyAssignedError,
    TicketClaimNotAllowedError,
    TicketPermissionError,
    TicketTeamAssignmentError,
    TicketTeamPermissionError,
)
from tests.ticket_service_fakes import FailingTeamAwareFakeDb, TeamAwareFakeDb


def make_claim_context(*, enabled=True, is_member=True, role="AGENT", **ticket_overrides):
    """Construye el escenario base de un ticket reclamable."""

    team = SimpleNamespace(id=uuid4(), self_assignment_enabled=enabled)
    agent = SimpleNamespace(id=uuid4(), role=role, is_active=True)
    ticket_data = {
        "id": uuid4(),
        "team_id": team.id,
        "assigned_to": None,
        "status": TicketStatus.OPEN,
        "archived_at": None,
    }
    ticket_data.update(ticket_overrides)
    ticket = SimpleNamespace(**ticket_data)
    membership = SimpleNamespace(team_id=team.id, user_id=agent.id) if is_member else None
    db = TeamAwareFakeDb(ticket=ticket, team=team, member=membership)
    return db, ticket, team, agent


def test_agent_claims_open_ticket_from_own_team():
    db, ticket, _, agent = make_claim_context()

    result = claim_ticket(db, ticket.id, agent)

    assert result is ticket
    assert ticket.assigned_to == agent.id
    assert agent.last_assigned_at is not None
    assert db.committed is True
    assert len(db.added) == 1
    history = db.added[0]
    assert isinstance(history, TicketAssignmentHistory)
    assert history.old_assigned_to is None
    assert history.new_assigned_to == agent.id
    assert history.changed_by == agent.id
    assert history.source == AssignmentSource.CLAIM


def test_admin_member_claims_open_ticket_from_own_team():
    db, ticket, _, admin = make_claim_context(role="ADMIN")

    result = claim_ticket(db, ticket.id, admin)

    assert result is ticket
    assert ticket.assigned_to == admin.id
    assert admin.last_assigned_at is not None
    history = db.added[0]
    assert history.new_assigned_to == admin.id
    assert history.changed_by == admin.id


def test_admin_without_membership_cannot_claim_team_ticket():
    db, ticket, _, admin = make_claim_context(role="ADMIN", is_member=False)

    with pytest.raises(TicketPermissionError, match="must belong"):
        claim_ticket(db, ticket.id, admin)

    assert ticket.assigned_to is None
    assert db.committed is False


def test_agent_cannot_claim_when_team_disables_self_assignment():
    db, ticket, _, agent = make_claim_context(enabled=False)

    with pytest.raises(TicketClaimNotAllowedError, match="disabled"):
        claim_ticket(db, ticket.id, agent)

    assert ticket.assigned_to is None
    assert db.committed is False


def test_agent_cannot_claim_ticket_from_another_team():
    db, ticket, _, agent = make_claim_context(is_member=False)

    with pytest.raises(TicketPermissionError, match="must belong"):
        claim_ticket(db, ticket.id, agent)

    assert ticket.assigned_to is None
    assert db.committed is False


@pytest.mark.parametrize(
    "ticket_overrides, error, message",
    [
        ({"assigned_to": uuid4()}, TicketAlreadyAssignedError, "already has"),
        ({"status": TicketStatus.ON_HOLD}, TicketClaimNotAllowedError, "Only open"),
        ({"archived_at": object()}, TicketClaimNotAllowedError, "Archived"),
        ({"team_id": None}, TicketClaimNotAllowedError, "must belong to a team"),
    ],
)
def test_agent_cannot_claim_ticket_in_invalid_operational_state(ticket_overrides, error, message):
    db, ticket, _, agent = make_claim_context(**ticket_overrides)

    with pytest.raises(error, match=message):
        claim_ticket(db, ticket.id, agent)

    assert db.committed is False


def test_inactive_agent_cannot_claim_ticket():
    db, ticket, _, agent = make_claim_context()
    agent.is_active = False

    with pytest.raises(TicketPermissionError, match="active operational users"):
        claim_ticket(db, ticket.id, agent)

    assert ticket.assigned_to is None


def test_assign_ticket_updates_ticket_and_creates_history():
    """
    Caso exitoso:
    - Un ADMIN asigna un ticket a un AGENT.
    - El ticket cambia su assigned_to.
    - Se crea un registro en TicketAssignmentHistory.
    - Se confirma la transaccion.
    """

    old_assigned_id = uuid4()
    team_id = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        assigned_to=old_assigned_id,
        team_id=team_id,
    )

    current_user = SimpleNamespace(
        id=uuid4(),
        role="ADMIN",
    )

    assigned_user = SimpleNamespace(
        id=uuid4(),
        role="AGENT",
        is_active=True,
    )
    db = TeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_user,
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user.id),
    )

    result = assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert result is ticket
    assert ticket.assigned_to == assigned_user.id
    assert assigned_user.last_assigned_at is not None
    assert db.committed is True
    assert db.refreshed is ticket
    assert db.rolled_back is False
    assert db.ticket_for_update_count == 1
    assert len(db.added) == 1

    history = db.added[0]
    assert isinstance(history, TicketAssignmentHistory)
    assert history.ticket_id == ticket.id
    assert history.old_assigned_to == old_assigned_id
    assert history.new_assigned_to == assigned_user.id
    assert history.changed_by == current_user.id
    assert history.source == AssignmentSource.MANUAL


def test_cannot_assign_teamless_ticket_to_active_admin():
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=None)
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    assigned_admin = SimpleNamespace(id=uuid4(), role="ADMIN", is_active=True)
    db = TeamAwareFakeDb(ticket=ticket, assigned_user=assigned_admin)

    with pytest.raises(TicketTeamAssignmentError, match="must belong to a team"):
        assign_ticket(db, ticket.id, assigned_admin.id, current_user)

    assert ticket.assigned_to is None
    assert db.added == []
    assert db.committed is False


def test_assign_ticket_raises_permission_error_when_user_cannot_assign():
    """
    Caso invalido:
    - Un USER intenta asignar un ticket.
    - La regla no lo permite.
    - El service lanza TicketPermissionError.
    - No modifica el ticket.
    - No crea historial.
    """

    team_id = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        assigned_to=None,
        team_id=team_id,
    )

    current_user = SimpleNamespace(
        id=uuid4(),
        role="USER",
    )

    assigned_user = SimpleNamespace(
        id=uuid4(),
        role="AGENT",
        is_active=True,
    )
    db = TeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_user,
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user.id),
    )

    with pytest.raises(TicketPermissionError):
        assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert ticket.assigned_to is None
    assert db.added == []
    assert db.committed is False
    assert db.refreshed is None
    assert db.rolled_back is True


def test_assign_ticket_raises_specific_error_when_agent_is_already_assigned():
    """
    Si no cambia el agente asignado, no hay accion real para auditar.
    """

    assigned_user_id = uuid4()

    team_id = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        assigned_to=assigned_user_id,
        team_id=team_id,
    )

    current_user = SimpleNamespace(
        id=uuid4(),
        role="ADMIN",
    )

    assigned_user = SimpleNamespace(
        id=assigned_user_id,
        role="AGENT",
        is_active=True,
    )
    db = TeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_user,
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user.id),
    )

    with pytest.raises(TicketAlreadyAssignedError, match="Ticket already assigned"):
        assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert ticket.assigned_to == assigned_user_id
    assert db.added == []
    assert db.committed is False
    assert db.refreshed is None
    assert db.rolled_back is True


def test_assign_ticket_rejects_inactive_agent():
    team_id = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        assigned_to=None,
        team_id=team_id,
    )
    current_user = SimpleNamespace(
        id=uuid4(),
        role="ADMIN",
    )
    assigned_user = SimpleNamespace(
        id=uuid4(),
        role="AGENT",
        is_active=False,
    )
    db = TeamAwareFakeDb(ticket=ticket, assigned_user=assigned_user)

    with pytest.raises(InvalidAssignedUserError, match="must be active"):
        assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert ticket.assigned_to is None
    assert db.added == []
    assert db.committed is False


def test_assign_ticket_rejects_inactive_admin():
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=uuid4())
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    assigned_admin = SimpleNamespace(id=uuid4(), role="ADMIN", is_active=False)
    db = TeamAwareFakeDb(ticket=ticket, assigned_user=assigned_admin)

    with pytest.raises(InvalidAssignedUserError, match="must be active"):
        assign_ticket(db, ticket.id, assigned_admin.id, current_user)

    assert ticket.assigned_to is None
    assert db.added == []
    assert db.committed is False


def test_assign_ticket_rolls_back_when_commit_fails():
    """
    Si falla la persistencia, el service debe limpiar la sesion.
    """

    team_id = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        assigned_to=None,
        team_id=team_id,
    )

    current_user = SimpleNamespace(
        id=uuid4(),
        role="ADMIN",
    )

    assigned_user = SimpleNamespace(
        id=uuid4(),
        role="AGENT",
        is_active=True,
    )
    db = FailingTeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_user,
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user.id),
    )

    with pytest.raises(RuntimeError, match="Commit failed"):
        assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert db.rolled_back is True


def test_team_lead_assigns_ticket_to_team_member():
    team_id = uuid4()
    lead_id = uuid4()
    assigned_user_id = uuid4()
    db = TeamAwareFakeDb(
        ticket=SimpleNamespace(id=uuid4(), assigned_to=None, team_id=team_id),
        assigned_user=SimpleNamespace(id=assigned_user_id, role="AGENT", is_active=True),
        team=SimpleNamespace(id=team_id),
        team_lead=SimpleNamespace(id=uuid4(), team_id=team_id, user_id=lead_id),
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user_id),
    )
    ticket = db.ticket
    current_user = SimpleNamespace(id=lead_id, role="AGENT")
    assigned_user = db.assigned_user

    result = assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert result is ticket
    assert ticket.assigned_to == assigned_user_id
    assert db.committed is True
    assert isinstance(db.added[0], TicketAssignmentHistory)


def test_admin_assigns_ticket_to_team_member():
    team_id = uuid4()
    assigned_user_id = uuid4()
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=team_id)
    assigned_user = SimpleNamespace(id=assigned_user_id, role="AGENT", is_active=True)
    db = TeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_user,
        member=SimpleNamespace(team_id=team_id, user_id=assigned_user_id),
    )
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")

    result = assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert result is ticket
    assert ticket.assigned_to == assigned_user_id
    assert db.committed is True
    assert isinstance(db.added[0], TicketAssignmentHistory)


def test_team_lead_assigns_ticket_to_admin_member():
    team_id = uuid4()
    lead_id = uuid4()
    assigned_admin_id = uuid4()
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=team_id)
    assigned_admin = SimpleNamespace(id=assigned_admin_id, role="ADMIN", is_active=True)
    db = TeamAwareFakeDb(
        ticket=ticket,
        assigned_user=assigned_admin,
        team_lead=SimpleNamespace(id=uuid4(), team_id=team_id, user_id=lead_id),
        member=SimpleNamespace(team_id=team_id, user_id=assigned_admin_id),
    )
    current_user = SimpleNamespace(id=lead_id, role="AGENT")

    result = assign_ticket(db, ticket.id, assigned_admin.id, current_user)

    assert result is ticket
    assert ticket.assigned_to == assigned_admin_id
    assert db.committed is True
    assert isinstance(db.added[0], TicketAssignmentHistory)


def test_admin_cannot_assign_ticket_to_agent_outside_team():
    team_id = uuid4()
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=team_id)
    assigned_user = SimpleNamespace(id=uuid4(), role="AGENT", is_active=True)
    db = TeamAwareFakeDb(ticket=ticket, assigned_user=assigned_user, member=None)
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")

    with pytest.raises(TicketPermissionError, match="invalid assignment"):
        assign_ticket(db, ticket.id, assigned_user.id, current_user)

    assert ticket.assigned_to is None
    assert db.added == []
    assert db.committed is False


def test_assign_ticket_to_team_updates_ticket_team():
    team = SimpleNamespace(
        id=uuid4(),
        auto_assignment_enabled=False,
        auto_assignment_delay_minutes=0,
        assignment_strategy=AssignmentStrategy.LEAST_ACTIVE,
    )
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=None)
    db = TeamAwareFakeDb(ticket=ticket, team=team)
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")

    result = assign_ticket_to_team(db, ticket.id, team.id, current_user)

    assert result is ticket
    assert ticket.team_id == team.id
    assert db.committed is True
    assert isinstance(db.added[0], TicketTeamHistory)
    assert db.added[0].old_team_id is None
    assert db.added[0].new_team_id == team.id
    assert db.added[0].changed_by == current_user.id
    assert db.added[0].source == AssignmentSource.MANUAL
    assert db.ticket_for_update_count == 1


def test_assign_ticket_to_same_team_raises_error():
    team = SimpleNamespace(id=uuid4())
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=team.id)
    db = TeamAwareFakeDb(ticket=ticket, team=team)
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")

    with pytest.raises(TicketTeamAssignmentError, match="already assigned"):
        assign_ticket_to_team(db, ticket.id, team.id, current_user)


def test_assign_ticket_to_team_rejects_assigned_user_outside_target_team():
    assigned_user_id = uuid4()
    team = SimpleNamespace(id=uuid4())
    ticket = SimpleNamespace(id=uuid4(), assigned_to=assigned_user_id, team_id=None)
    db = TeamAwareFakeDb(ticket=ticket, team=team, member=None)
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")

    with pytest.raises(TicketTeamAssignmentError, match="does not belong"):
        assign_ticket_to_team(db, ticket.id, team.id, current_user)

    assert ticket.team_id is None
    assert db.committed is False


def test_team_lead_assigns_unassigned_ticket_to_own_team_when_category_matches():
    team_id = uuid4()
    category_id = uuid4()
    lead_id = uuid4()
    team = SimpleNamespace(
        id=team_id,
        auto_assignment_enabled=False,
        auto_assignment_delay_minutes=0,
        assignment_strategy=AssignmentStrategy.LEAST_ACTIVE,
    )
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=None, category_id=category_id)
    category_team = SimpleNamespace(id=uuid4(), category_id=category_id, team_id=team_id)
    current_user = SimpleNamespace(id=lead_id, role="AGENT")
    team_lead = SimpleNamespace(id=uuid4(), team_id=team_id, user_id=lead_id)
    db = TeamAwareFakeDb(ticket=ticket, team=team, category_team=category_team, team_lead=team_lead)

    result = assign_ticket_to_team(db, ticket.id, team.id, current_user)

    assert result is ticket
    assert ticket.team_id == team.id
    assert db.committed is True
    assert isinstance(db.added[0], TicketTeamHistory)


def test_team_lead_cannot_assign_ticket_if_category_is_not_associated():
    team_id = uuid4()
    category_id = uuid4()
    lead_id = uuid4()
    team = SimpleNamespace(id=team_id)
    ticket = SimpleNamespace(id=uuid4(), assigned_to=None, team_id=None, category_id=category_id)
    current_user = SimpleNamespace(id=lead_id, role="AGENT")
    team_lead = SimpleNamespace(id=uuid4(), team_id=team_id, user_id=lead_id)
    db = TeamAwareFakeDb(ticket=ticket, team=team, category_team=None, team_lead=team_lead)

    with pytest.raises(TicketTeamPermissionError, match="category is not associated"):
        assign_ticket_to_team(db, ticket.id, team.id, current_user)

    assert ticket.team_id is None
    assert db.committed is False


def test_admin_changes_ticket_category_and_clears_team_and_assignee():
    old_category_id = uuid4()
    new_category = SimpleNamespace(
        id=uuid4(),
        is_active=True,
        auto_team_assignment_enabled=False,
        team_assignment_delay_minutes=0,
        team_assignment_strategy=TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )
    old_team_id = uuid4()
    old_assigned_to = uuid4()
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.IN_PROGRESS,
        category_id=old_category_id,
        team_id=old_team_id,
        assigned_to=old_assigned_to,
    )
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    db = TeamAwareFakeDb(ticket=ticket, category=new_category)

    result = change_ticket_category(db, ticket.id, new_category.id, current_user, " Categoria incorrecta ")

    assert result is ticket
    assert ticket.category_id == new_category.id
    assert ticket.team_id is None
    assert ticket.assigned_to is None
    assert db.committed is True
    assert db.ticket_for_update_count == 1

    category_history = db.added[0]
    assert isinstance(category_history, TicketCategoryHistory)
    assert category_history.old_category_id == old_category_id
    assert category_history.new_category_id == new_category.id
    assert category_history.reason == "Categoria incorrecta"
    assert category_history.changed_by == current_user.id

    team_history = db.added[1]
    assert isinstance(team_history, TicketTeamHistory)
    assert team_history.old_team_id == old_team_id
    assert team_history.new_team_id is None
    assert team_history.source == AssignmentSource.MANUAL

    assignment_history = db.added[2]
    assert isinstance(assignment_history, TicketAssignmentHistory)
    assert assignment_history.old_assigned_to == old_assigned_to
    assert assignment_history.new_assigned_to is None
    assert assignment_history.source == AssignmentSource.MANUAL


def test_change_ticket_category_requires_reason():
    new_category = SimpleNamespace(id=uuid4(), is_active=True)
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.OPEN,
        category_id=uuid4(),
        team_id=None,
        assigned_to=None,
    )
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    db = TeamAwareFakeDb(ticket=ticket, category=new_category)

    with pytest.raises(MissingCategoryChangeReasonError, match="Reason is required"):
        change_ticket_category(db, ticket.id, new_category.id, current_user, "   ")

    assert ticket.category_id != new_category.id
    assert db.added == []
    assert db.committed is False


def test_change_ticket_category_rejects_inactive_category():
    new_category = SimpleNamespace(id=uuid4(), is_active=False)
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.OPEN,
        category_id=uuid4(),
        team_id=None,
        assigned_to=None,
    )
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    db = TeamAwareFakeDb(ticket=ticket, category=new_category)

    with pytest.raises(InvalidTicketCategoryError, match="Inactive categories"):
        change_ticket_category(db, ticket.id, new_category.id, current_user, "Correccion")

    assert db.added == []
    assert db.committed is False


def test_change_ticket_category_rejects_same_category():
    category_id = uuid4()
    new_category = SimpleNamespace(id=category_id, is_active=True)
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.OPEN,
        category_id=category_id,
        team_id=None,
        assigned_to=None,
    )
    current_user = SimpleNamespace(id=uuid4(), role="ADMIN")
    db = TeamAwareFakeDb(ticket=ticket, category=new_category)

    with pytest.raises(InvalidTicketCategoryChangeError, match="already belongs"):
        change_ticket_category(db, ticket.id, new_category.id, current_user, "Correccion")

    assert db.added == []
    assert db.committed is False


def test_team_lead_changes_category_for_ticket_in_own_team():
    team_id = uuid4()
    lead_id = uuid4()
    new_category = SimpleNamespace(
        id=uuid4(),
        is_active=True,
        auto_team_assignment_enabled=False,
        team_assignment_delay_minutes=0,
        team_assignment_strategy=TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.ON_HOLD,
        category_id=uuid4(),
        team_id=team_id,
        assigned_to=None,
    )
    current_user = SimpleNamespace(id=lead_id, role="AGENT")
    team = SimpleNamespace(id=team_id)
    team_lead = SimpleNamespace(id=uuid4(), team_id=team_id, user_id=lead_id)
    db = TeamAwareFakeDb(ticket=ticket, category=new_category, team=team, team_lead=team_lead)

    result = change_ticket_category(db, ticket.id, new_category.id, current_user, "No corresponde al team")

    assert result is ticket
    assert ticket.category_id == new_category.id
    assert ticket.team_id is None
    assert db.committed is True
    assert isinstance(db.added[0], TicketCategoryHistory)
    assert isinstance(db.added[1], TicketTeamHistory)


def test_agent_without_team_lead_permission_cannot_change_category():
    new_category = SimpleNamespace(id=uuid4(), is_active=True)
    ticket = SimpleNamespace(
        id=uuid4(),
        status=TicketStatus.ON_HOLD,
        category_id=uuid4(),
        team_id=uuid4(),
        assigned_to=None,
    )
    current_user = SimpleNamespace(id=uuid4(), role="AGENT")
    db = TeamAwareFakeDb(ticket=ticket, category=new_category, team=None)

    with pytest.raises(TicketPermissionError, match="Only the current team lead"):
        change_ticket_category(db, ticket.id, new_category.id, current_user, "No corresponde")

    assert db.added == []
    assert db.committed is False
