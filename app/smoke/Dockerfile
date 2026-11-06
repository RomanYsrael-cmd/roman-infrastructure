FROM python:3.13-slim

ARG SOURCE_REPO
ARG REVISION
ARG PLATFORM_REF

LABEL org.opencontainers.image.source="${SOURCE_REPO}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.title="Roman Infrastructure Smoke Service" \
      io.roman.platform.source-revision="${PLATFORM_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py /app/server.py
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=2s --start-period=3s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=1)"
CMD ["python", "/app/server.py"]
