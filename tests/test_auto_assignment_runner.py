"""Pruebas del runner que coordina Redis, sesión SQL y service."""

import pytest

from app.core.config import settings
from app.services.ticket_auto_assignment_service import AutoAssignmentResult
from app.workers.auto_assignment_runner import (
    AUTO_ASSIGNMENT_LOCK_NAME,
    AutoAssignmentRunStatus,
    run_auto_assignment_batch,
)


class FakeLock:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.acquire_calls = []
        self.release_calls = 0

    def acquire(self, *, blocking):
        self.acquire_calls.append(blocking)
        return self.acquired

    def release(self):
        self.release_calls += 1


class FakeRedisClient:
    def __init__(self, lock):
        self.current_lock = lock
        self.lock_calls = []
        self.closed = False

    def lock(self, name, *, timeout):
        self.lock_calls.append((name, timeout))
        return self.current_lock

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _empty_assignment_result():
    return AutoAssignmentResult(
        teams_assigned=0,
        users_assigned=0,
        teams_without_candidate=0,
        users_without_candidate=0,
        skipped=0,
        errors=0,
    )


def test_runner_processes_batch_and_releases_resources():
    """Con el lock disponible ejecuta el service y limpia ambos recursos."""

    lock = FakeLock(acquired=True)
    redis_client = FakeRedisClient(lock)
    db = FakeSession()
    processor_calls = []
    expected = _empty_assignment_result()

    def fake_processor(current_db, *, limit):
        processor_calls.append((current_db, limit))
        return expected

    result = run_auto_assignment_batch(
        session_factory=lambda: db,
        redis_client_factory=lambda: redis_client,
        processor=fake_processor,
    )

    assert result.status == AutoAssignmentRunStatus.COMPLETED
    assert result.assignments is expected
    assert processor_calls == [(db, settings.auto_assignment_batch_size_per_phase)]
    assert redis_client.lock_calls == [
        (AUTO_ASSIGNMENT_LOCK_NAME, settings.auto_assignment_lock_timeout_seconds)
    ]
    assert lock.acquire_calls == [False]
    assert lock.release_calls == 1
    assert db.closed is True
    assert redis_client.closed is True


def test_runner_skips_without_opening_database_when_lock_is_held():
    """Otra ejecución activa impide iniciar trabajo o abrir una sesión SQL."""

    lock = FakeLock(acquired=False)
    redis_client = FakeRedisClient(lock)
    session_factory_calls = []
    processor_calls = []

    def fake_session_factory():
        session_factory_calls.append(True)
        return FakeSession()

    def fake_processor(db, *, limit):
        processor_calls.append((db, limit))
        return _empty_assignment_result()

    result = run_auto_assignment_batch(
        session_factory=fake_session_factory,
        redis_client_factory=lambda: redis_client,
        processor=fake_processor,
    )

    assert result.status == AutoAssignmentRunStatus.SKIPPED_LOCK_HELD
    assert result.assignments is None
    assert session_factory_calls == []
    assert processor_calls == []
    assert lock.release_calls == 0
    assert redis_client.closed is True


def test_runner_releases_lock_and_session_when_processor_fails():
    """Una excepción se propaga, pero no deja recursos abiertos."""

    lock = FakeLock(acquired=True)
    redis_client = FakeRedisClient(lock)
    db = FakeSession()

    def failing_processor(db, *, limit):
        raise RuntimeError("Batch failed")

    with pytest.raises(RuntimeError, match="Batch failed"):
        run_auto_assignment_batch(
            session_factory=lambda: db,
            redis_client_factory=lambda: redis_client,
            processor=failing_processor,
        )

    assert db.closed is True
    assert lock.release_calls == 1
    assert redis_client.closed is True
