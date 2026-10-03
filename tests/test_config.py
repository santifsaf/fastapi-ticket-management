"""Validaciones de configuración que protegen la ejecución en background."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql://user:password@localhost/ticketing",
        "secret_key": "test-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_auto_assignment_worker_defaults_are_safe():
    worker_settings = make_settings()

    assert worker_settings.auto_assignment_poll_seconds == 60
    assert worker_settings.auto_assignment_batch_size_per_phase == 100
    assert worker_settings.auto_assignment_task_time_limit_seconds == 240
    assert worker_settings.auto_assignment_lock_timeout_seconds == 300


def test_lock_timeout_must_be_greater_than_task_time_limit():
    with pytest.raises(ValidationError, match="lock timeout must be greater"):
        make_settings(
            auto_assignment_task_time_limit_seconds=300,
            auto_assignment_lock_timeout_seconds=300,
        )


def test_auto_assignment_batch_size_must_be_positive():
    with pytest.raises(ValidationError):
        make_settings(auto_assignment_batch_size_per_phase=0)
