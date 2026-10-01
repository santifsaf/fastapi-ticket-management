"""Tests unitarios del selector de responsables para autoasignación."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models.category import TeamAssignmentStrategy
from app.models.team import AssignmentStrategy
from app.services.assignment_timing import calculate_assignment_due_at
from app.services import ticket_auto_assignment_service
from app.services.ticket_auto_assignment_service import (
    AssignmentOutcome,
    AutoAssignmentCandidate,
    TeamAssignmentCandidate,
    choose_auto_assignment_candidate,
    choose_team_assignment_candidate,
    process_due_auto_assignments,
)


def make_candidate(user_id: int, active_tickets: int, last_assigned_at=None):
    user = SimpleNamespace(
        id=UUID(int=user_id),
        last_assigned_at=last_assigned_at,
    )
    return AutoAssignmentCandidate(user=user, active_ticket_count=active_tickets)


def test_least_active_selects_user_with_fewer_active_tickets():
    busy = make_candidate(1, active_tickets=3)
    available = make_candidate(2, active_tickets=1)

    result = choose_auto_assignment_candidate(
        [busy, available],
        AssignmentStrategy.LEAST_ACTIVE,
    )

    assert result is available.user


def test_least_active_uses_longest_idle_as_tie_breaker():
    now = datetime.now(timezone.utc)
    recently_assigned = make_candidate(1, 1, now - timedelta(minutes=5))
    longest_idle = make_candidate(2, 1, now - timedelta(hours=2))

    result = choose_auto_assignment_candidate(
        [recently_assigned, longest_idle],
        AssignmentStrategy.LEAST_ACTIVE,
    )

    assert result is longest_idle.user


def test_longest_idle_prioritizes_user_never_assigned():
    previously_assigned = make_candidate(1, 0, datetime.now(timezone.utc) - timedelta(days=1))
    never_assigned = make_candidate(2, 2, None)

    result = choose_auto_assignment_candidate(
        [previously_assigned, never_assigned],
        AssignmentStrategy.LONGEST_IDLE,
    )

    assert result is never_assigned.user


def test_longest_idle_uses_active_load_as_tie_breaker():
    same_time = datetime.now(timezone.utc) - timedelta(hours=1)
    busier = make_candidate(1, 3, same_time)
    less_busy = make_candidate(2, 1, same_time)

    result = choose_auto_assignment_candidate(
        [busier, less_busy],
        AssignmentStrategy.LONGEST_IDLE,
    )

    assert result is less_busy.user


def test_selector_returns_none_when_team_has_no_eligible_candidates():
    result = choose_auto_assignment_candidate([], AssignmentStrategy.LEAST_ACTIVE)

    assert result is None


def test_team_selector_uses_load_proportional_to_active_members():
    small_team = SimpleNamespace(id=UUID(int=1))
    large_team = SimpleNamespace(id=UUID(int=2))
    small_team_candidate = TeamAssignmentCandidate(
        team=small_team,
        active_ticket_count=3,
        active_member_count=2,
    )
    large_team_candidate = TeamAssignmentCandidate(
        team=large_team,
        active_ticket_count=4,
        active_member_count=4,
    )

    result = choose_team_assignment_candidate(
        [small_team_candidate, large_team_candidate],
        TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )

    assert result is large_team


def test_team_selector_uses_total_tickets_as_proportional_load_tie_breaker():
    busier_team = SimpleNamespace(id=UUID(int=1))
    less_busy_team = SimpleNamespace(id=UUID(int=2))
    busier_candidate = TeamAssignmentCandidate(busier_team, 4, 4)
    less_busy_candidate = TeamAssignmentCandidate(less_busy_team, 2, 2)

    result = choose_team_assignment_candidate(
        [busier_candidate, less_busy_candidate],
        TeamAssignmentStrategy.LEAST_LOAD_PER_MEMBER,
    )

    assert result is less_busy_team


def test_assignment_due_at_uses_delay_only_when_automation_is_enabled():
    now = datetime.now(timezone.utc)

    enabled_due_at = calculate_assignment_due_at(True, 20, now=now)
    disabled_due_at = calculate_assignment_due_at(False, 20, now=now)

    assert enabled_due_at == now + timedelta(minutes=20)
    assert disabled_due_at is None


class FakeBatchDb:
    def __init__(self):
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1


def test_batch_continues_after_individual_errors_and_counts_each_outcome(
    monkeypatch,
):
    """Un ticket defectuoso no impide procesar los siguientes de la tanda."""

    team_ids = [UUID(int=value) for value in range(1, 5)]
    user_ids = [UUID(int=value) for value in range(5, 8)]
    processed_ids = []
    logged_errors = []
    db = FakeBatchDb()

    class FakeLogger:
        def exception(self, message, *, extra):
            logged_errors.append((message, extra))

    def fake_find_due_team_ticket_ids(db, current_time, limit):
        return team_ids

    def fake_find_due_user_ticket_ids(db, current_time, limit):
        return user_ids

    monkeypatch.setattr(
        ticket_auto_assignment_service,
        "_find_due_team_ticket_ids",
        fake_find_due_team_ticket_ids,
    )
    monkeypatch.setattr(
        ticket_auto_assignment_service,
        "_find_due_user_ticket_ids",
        fake_find_due_user_ticket_ids,
    )

    def fake_team_executor(db, ticket_id, *, now):
        processed_ids.append(ticket_id)
        outcomes = {
            team_ids[0]: AssignmentOutcome.ASSIGNED,
            team_ids[2]: AssignmentOutcome.NO_CANDIDATE,
            team_ids[3]: AssignmentOutcome.SKIPPED,
        }
        if ticket_id == team_ids[1]:
            raise RuntimeError("Team assignment failed")
        return outcomes[ticket_id]

    def fake_user_executor(db, ticket_id, *, now):
        processed_ids.append(ticket_id)
        if ticket_id == user_ids[2]:
            raise RuntimeError("User assignment failed")
        return (
            AssignmentOutcome.ASSIGNED
            if ticket_id == user_ids[0]
            else AssignmentOutcome.NO_CANDIDATE
        )

    monkeypatch.setattr(
        ticket_auto_assignment_service,
        "auto_assign_ticket_to_team",
        fake_team_executor,
    )
    monkeypatch.setattr(
        ticket_auto_assignment_service,
        "auto_assign_ticket_to_user",
        fake_user_executor,
    )
    monkeypatch.setattr(ticket_auto_assignment_service, "logger", FakeLogger())

    result = process_due_auto_assignments(db)

    assert processed_ids == team_ids + user_ids
    assert result.teams_assigned == 1
    assert result.users_assigned == 1
    assert result.teams_without_candidate == 1
    assert result.users_without_candidate == 1
    assert result.skipped == 1
    assert result.errors == 2
    assert db.rollback_calls == 2
    assert len(logged_errors) == 2
    assert logged_errors[0][1]["assignment_phase"] == "TEAM"
    assert logged_errors[1][1]["assignment_phase"] == "USER"


def test_batch_propagates_error_when_due_ticket_query_cannot_start(monkeypatch):
    """Una caída general de base debe fallar la ejecución completa."""

    def fail_query(db, current_time, limit):
        raise ConnectionError("Database unavailable")

    monkeypatch.setattr(
        ticket_auto_assignment_service,
        "_find_due_team_ticket_ids",
        fail_query,
    )

    with pytest.raises(ConnectionError, match="Database unavailable"):
        process_due_auto_assignments(FakeBatchDb())
