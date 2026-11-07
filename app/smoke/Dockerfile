FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

ARG PACKAGE_REPO
ARG SOURCE_REPO
ARG REVISION
ARG PLATFORM_REF
ARG CALLER_REVISION
ARG SMOKE_HEALTH_MODE=healthy

LABEL org.opencontainers.image.source="${PACKAGE_REPO}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.title="Roman Infrastructure Smoke Service" \
      io.roman.platform.source-repository="${SOURCE_REPO}" \
      io.roman.platform.source-revision="${PLATFORM_REF}" \
      io.roman.deployment.package-repository="${PACKAGE_REPO}" \
      io.roman.deployment.caller-revision="${CALLER_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMOKE_HEALTH_MODE=${SMOKE_HEALTH_MODE}

WORKDIR /app
COPY server.py /app/server.py
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --start-period=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=1)"
CMD ["python", "/app/server.py"]
