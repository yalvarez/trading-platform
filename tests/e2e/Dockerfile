# tests/e2e/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Docker CLI is required for vps_observer.grep_container_logs/restart_container
# to call `docker logs`/`docker restart` on sibling containers — mount the
# host's docker socket at run time (see docker-compose.yml e2e_runner
# service). docker-cli is the package that actually provides the `docker`
# binary (as of Debian 13/trixie, docker.io only installs dockerd/docker-init,
# not the client — discovered running this image for real; docker.io alone
# left the container with no `docker` binary on PATH at all).
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

COPY services/ ./services/
COPY tests/ ./tests/
RUN pip install --no-cache-dir -r ./tests/e2e/requirements.txt

ENV PYTHONPATH="/app"

WORKDIR /app

ENTRYPOINT ["python", "-m", "tests.e2e.runner"]
