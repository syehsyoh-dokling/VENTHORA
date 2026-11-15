FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[solver]"
COPY data ./data
COPY policy ./policy
# the service never runs as root: its own user owns only what it writes (the stores under /app/data)
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin venthora \
    && chown -R venthora:venthora /app/data
USER venthora
ENV VENTHORA_AUDIT_DB=/app/data/venthora_audit.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "venthora.api:app", "--host", "0.0.0.0", "--port", "8000"]
