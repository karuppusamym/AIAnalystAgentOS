import os

SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "analystos-dev-superset-secret-change-me")
SQLALCHEMY_DATABASE_URI = os.environ.get("SUPERSET_DATABASE_URI", "postgresql+psycopg2://superset:superset@postgres:5432/superset")
WTF_CSRF_ENABLED = True
WTF_CSRF_EXEMPT_LIST = ["superset.views.core.log", "superset.charts.data.api.data"]
FEATURE_FLAGS = {"DASHBOARD_NATIVE_FILTERS": True, "ENABLE_TEMPLATE_PROCESSING": False}
TALISMAN_ENABLED = False
ENABLE_PROXY_FIX = True
# Allow embedding previews from the AnalystOS UI during development.
HTTP_HEADERS = {"X-Frame-Options": "SAMEORIGIN"}
