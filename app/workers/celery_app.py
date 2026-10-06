"""Aplicación Celery y configuración compartida por workers y Beat."""

from celery import Celery

from app.core.config import settings


# Este objeto registra la configuración de Celery. Crear la aplicación no abre
# una conexión permanente ni inicia un worker; eso sucede al ejecutar la CLI.
celery_app = Celery(
    "ticketing",
    broker=settings.celery_broker_url,
)

celery_app.conf.update(
    # Solo aceptamos JSON para no deserializar objetos Python arbitrarios.
    task_serializer="json",
    accept_content=["json"],
    # La autoasignación no necesita guardar un valor de retorno en Redis.
    task_ignore_result=True,
    # Los mensajes y futuras planificaciones se interpretan en UTC.
    enable_utc=True,
    timezone="UTC",
    # Si Redis arranca después del worker, Celery reintenta la conexión inicial.
    broker_connection_retry_on_startup=True,
)
