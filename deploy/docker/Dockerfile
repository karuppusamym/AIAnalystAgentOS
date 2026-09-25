# AnalystOS application image: API, Temporal worker and the ServiceNow mock share it.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
# Optional build secret `pipcert`: CA bundle for TLS-inspecting proxies.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir -e . && useradd --create-home --uid 10001 analystos && mkdir -p /app/var && chown -R analystos /app/var
USER analystos
EXPOSE 8000
CMD ["uvicorn", "analystos.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
