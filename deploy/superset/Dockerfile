# apache/superset 4.1.x images no longer bundle a PostgreSQL driver. Superset needs one for its
# own metadata DB and for the AnalystOS analytics connection.
FROM apache/superset:4.1.1
USER root
# Optional build secret `pipcert`: a CA bundle for environments behind a TLS-inspecting proxy.
RUN --mount=type=secret,id=pipcert,required=false \
    if [ -f /run/secrets/pipcert ]; then export PIP_CERT=/run/secrets/pipcert; fi; \
    pip install --no-cache-dir psycopg2-binary==2.9.9
USER superset
