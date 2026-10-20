# Una sola imagen ejecutará API, migraciones y procesos Celery.
FROM python:3.13-slim

# Evita archivos .pyc, muestra logs inmediatamente y reduce ruido de pip.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Las dependencias se instalan antes que el código para reutilizar esta capa
# mientras requirements.txt no cambie.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# El proceso no necesita permisos de administrador dentro del contenedor.
RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser

# Alembic necesita su configuración y migraciones; todos los procesos necesitan app.
COPY --chown=appuser:appgroup alembic.ini ./
COPY --chown=appuser:appgroup alembic ./alembic
COPY --chown=appuser:appgroup app ./app

USER appuser

EXPOSE 8000

# Compose podrá reemplazar este comando para ejecutar Alembic, Worker o Beat.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
