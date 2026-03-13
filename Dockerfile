FROM python:3.12-slim

LABEL org.opencontainers.image.title="oci-free-tier-docker-capacity-watch" \
      org.opencontainers.image.description="Dockerized OCI Always Free capacity watcher that retries and provisions VM targets when capacity becomes available." \
      org.opencontainers.image.source="https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch" \
      org.opencontainers.image.url="https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
COPY --chmod=755 worker /app/worker

ENTRYPOINT ["/app/worker/entrypoint.sh"]
