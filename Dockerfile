FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[solver]"
COPY data ./data
COPY policy ./policy
ENV VENTHORA_AUDIT_DB=/app/data/venthora_audit.db
EXPOSE 8000
CMD ["uvicorn", "venthora.api:app", "--host", "0.0.0.0", "--port", "8000"]
