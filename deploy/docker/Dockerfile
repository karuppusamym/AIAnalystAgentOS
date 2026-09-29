# AnalystOS application image (API, Temporal worker, scheduler). ADR-0025: the extras decide its weight.
#   lite API (default):   docker build --build-arg EXTRAS=reports,ml ...        core + PDF/XLSX reports + ML
#   standard API:         --build-arg EXTRAS=reports,temporal,ml                  + the Temporal client
#   worker / scheduler:   --build-arg EXTRAS=standard                             + ml, graph (every extra)
# The ML stack (scikit-learn, statsmodels) is in the API image too: ML experiments train in the lite API and are
# checked there before they are dispatched (compose default; ADR-0022's isolated compute-ml pool is opt-in). Built
# without it (EXTRAS=reports) the driver model, experiments and Holt-Winters forecasts report "unavailable".
# The ServiceNow demo mock has its own small image (Dockerfile.mock).
FROM python:3.11-slim
ARG EXTRAS=reports
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
# Domain packs (glossary, hints, hypothesis templates) and the connector certification evidence the
# API derives `certified` from.
COPY packs ./packs
COPY docs/60-delivery/evidence ./docs/60-delivery/evidence
# Optional build secret `pipcert`: CA bundle for TLS-inspecting proxies.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    if [ -n "$EXTRAS" ]; then target=".[$EXTRAS]"; else target="."; fi; \
    pip install --no-cache-dir -e "$target" && useradd --create-home --uid 10001 analystos && mkdir -p /app/var && chown -R analystos /app/var
USER analystos
EXPOSE 8000
CMD ["uvicorn", "analystos.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
