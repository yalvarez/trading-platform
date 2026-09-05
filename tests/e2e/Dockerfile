# tests/e2e/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Docker CLI is required for vps_observer.grep_container_logs to call
# `docker logs` on sibling containers — mount the host's docker socket at
# run time (see docker-compose.yml e2e_runner service).
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY services/ ./services/
COPY tests/ ./tests/
RUN pip install --no-cache-dir -r ./tests/e2e/requirements.txt

ENV PYTHONPATH="/app"

WORKDIR /app

ENTRYPOINT ["python", "-m", "tests.e2e.runner"]
