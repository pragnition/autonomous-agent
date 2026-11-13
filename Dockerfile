FROM python:3.12-slim AS base
RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
# Install the pinned runtime dependencies straight from pyproject.toml, so versions live in one place
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt

FROM base AS dev
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" > /tmp/req-dev.txt \
 && pip install --no-cache-dir -r /tmp/req-dev.txt
ENV PYTHONPATH=/app/src

FROM base AS app
COPY src /app/src
COPY scripts /app/scripts
ENV PYTHONPATH=/app/src

# core runs `az` for debug sessions (spec v2 §7); its login lives in /data/cli, never in a sandbox
FROM app AS core
RUN python -m venv /opt/az \
 && /opt/az/bin/pip install --no-cache-dir azure-cli \
 && ln -s /opt/az/bin/az /usr/local/bin/az
CMD ["python", "-m", "agent", "core"]

FROM app AS supervisor
CMD ["python", "-m", "agent", "supervisor"]

FROM alpine:3.20 AS relay
RUN apk add --no-cache socat
