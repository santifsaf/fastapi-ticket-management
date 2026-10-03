from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracion central cargada desde variables APP_* y el archivo .env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_")

    app_name: str = "Ticketing System"
    debug: bool = True 
    database_url: str
    # Nunca debe apuntar a la base de desarrollo. Si queda vacia, la
    # infraestructura de tests deriva un nombre terminado en "_test".
    test_database_url: str | None = None
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    archive_closed_tickets_after_days: int = 30

    # Redis actuará como broker de Celery y, mediante una conexión separada,
    # almacenará el lock que impide ejecutar dos tandas al mismo tiempo.
    celery_broker_url: str = "redis://localhost:6379/0"
    redis_url: str = "redis://localhost:6379/0"
    auto_assignment_poll_seconds: int = Field(default=60, gt=0)
    auto_assignment_batch_size_per_phase: int = Field(default=100, gt=0)
    auto_assignment_task_time_limit_seconds: int = Field(default=240, gt=0)
    auto_assignment_lock_timeout_seconds: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def validate_auto_assignment_timeouts(self):
        """El lock debe sobrevivir a la tarea para impedir solapamientos."""

        if (
            self.auto_assignment_lock_timeout_seconds
            <= self.auto_assignment_task_time_limit_seconds
        ):
            raise ValueError(
                "auto-assignment lock timeout must be greater than task time limit"
            )
        return self

settings = Settings()
