"""Error taxonomy (FND-014). Retryability is a property of the error class, not of call sites."""
from __future__ import annotations


class AnalystOSError(Exception):
    code = "internal_error"
    retryable = False
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class NotFound(AnalystOSError):
    code, http_status = "not_found", 404


class Conflict(AnalystOSError):
    code, http_status = "conflict", 409


class InvalidInput(AnalystOSError):
    code, http_status = "invalid_input", 422


class Unauthenticated(AnalystOSError):
    code, http_status = "unauthenticated", 401


class Forbidden(AnalystOSError):
    """Authorization or policy denial. Never retryable."""

    code, http_status = "forbidden", 403


class PolicyDenied(Forbidden):
    code = "policy_denied"


class ApprovalRequired(AnalystOSError):
    code, http_status = "approval_required", 409


class SQLRejected(AnalystOSError):
    """The gateway refused a statement (not read-only, out of scope, restricted column...)."""

    code, http_status = "sql_rejected", 422


class QueryTimeout(AnalystOSError):
    code, http_status, retryable = "query_timeout", 504, False


class UpstreamUnavailable(AnalystOSError):
    """A dependency (source, model provider, BI tool) failed transiently."""

    code, http_status, retryable = "upstream_unavailable", 503, True


class BudgetExceeded(AnalystOSError):
    code, http_status = "budget_exceeded", 429


class ModelRouteUnavailable(AnalystOSError):
    """No allowed provider could serve a model profile. Fail closed: never route elsewhere silently."""

    code, http_status, retryable = "model_route_unavailable", 503, True


class RunCancelled(AnalystOSError):
    code, http_status = "run_cancelled", 409


class ProviderQuotaExhausted(ModelRouteUnavailable):
    """The provider refused for payment/credit reasons (HTTP 402). Every model behind that provider
    will fail the same way, so the router stops trying it for a cooldown instead of burning calls."""

    code, retryable = "provider_quota_exhausted", False


class LLMDisabled(ModelRouteUnavailable):
    """An administrator set this purpose to `off` (or the prompt was refused as oversize).
    Callers take their deterministic path; this is a decision, not an outage."""

    code, retryable = "llm_disabled", False


class ContextOverBudget(AnalystOSError):
    """The mandatory part of a prompt's context alone exceeds the purpose's budget (context
    compiler, P4-T03). Callers take the deterministic path and record the refusal; the context is
    never cut to make it fit."""

    code, http_status = "context_over_budget", 422
