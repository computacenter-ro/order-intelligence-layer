# One image for all Python services (ai_service, backend, mock services,
# collector, injector). They share one codebase + requirements.txt and differ
# only in the command run — so docker-compose sets each service's `command`
# against this single image rather than maintaining near-identical Dockerfiles.
#
# The apps run natively in dev (see CLAUDE.md); this image is for compose /
# demo / CI runs where everything comes up with `docker compose up`.

FROM python:3.11-slim

# Faster, quieter Python in a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then the source (everything not excluded by .dockerignore).
COPY . .

# Run as a non-root user (least privilege).
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

# No default CMD: each compose service supplies its own `command`. Documented
# entrypoints:
#   ai_service : python -m ai_service.main
#   backend    : uvicorn backend.main:app --host 0.0.0.0 --port 8000
#   collector  : uvicorn pipeline.mock_es.app:app --host 0.0.0.0 --port 9200
#   mock svcs  : python -m pipeline.services.run_all
#   migrate    : alembic upgrade head
