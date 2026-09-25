"""Service-free checks for P4-C02 (budgets), P4-C03 (role naming), P4-C06 (critic family), P4-C11 (gate)."""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest

from analystos.connectors.naming import is_safe_identifier, workspace_reader_role
from analystos.core.errors import BudgetExceeded, PolicyDenied


# --------------------------------------------------------------------------- P4-C06 critic family
@pytest.mark.parametrize(("source", "expected"), [
    ("llm:openai/gpt-4.1-mini", "openai"),
    ("llm:anthropic/claude-sonnet-4.5", "anthropic"),
    ("llm:google/gemini-2.5-flash", "google"),
    ("template", None),          # deterministic wording: no author model, nothing to exclude
    ("llm:deterministic", None),
    ("llm:", None),
    ("", None),
])
def test_critic_excludes_the_family_that_actually_wrote_the_narrative(source, expected):
    from analystos.agents.critic import narrative_family

    assert narrative_family(source) == expected


def test_critic_no_longer_guesses_a_fixed_family():
    from analystos.agents import critic

    src = inspect.getsource(critic)
    assert '"anthropic"' not in src and "'anthropic'" not in src


# --------------------------------------------------------------------------- P4-C02 critic re-runs
def test_critic_reruns_use_the_budgeted_gated_runner_not_the_gateway():
    """Verification re-runs must go through ctx.run_sql (tool gate + per-run budget), never
    straight to gateway.execute, which skipped max_queries_per_run."""
    from analystos.agents import critic

    tree = ast.parse(inspect.getsource(critic))
    direct = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "execute"
              and isinstance(n.value, ast.Attribute) and n.value.attr == "gateway"]
    assert direct == []
    assert "verification.rerun" in inspect.getsource(critic.verify_insights)
    assert "use_cache=False" in inspect.getsource(critic.verify_insights)


class _Runner:
    dialect = "postgres"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, sql, **kw):  # noqa: ANN001, ANN003, ANN204
        self.calls.append({"sql": sql, **kw})
        return SimpleNamespace(result_hash="h")


def _ctx(monkeypatch, *, used: int, budget: int, denied: bool = False):
    """A RunContext with the DB-touching hooks replaced, to exercise the run_sql wrapper alone."""
    from analystos.contracts.policy import WorkspacePolicyDoc
    from analystos.runtime import context as ctx_mod

    runner = _Runner()
    gateway = SimpleNamespace(run_sql_for=lambda scope, **kw: runner)
    ctx = ctx_mod.RunContext(run=SimpleNamespace(id="run_1"), task=SimpleNamespace(id="t_1", key="verify", plan_version=1),
                             user=SimpleNamespace(id="u_1"), workspace=SimpleNamespace(id="ws_1"),
                             policy=WorkspacePolicyDoc(max_queries_per_run=budget), scope=SimpleNamespace(),
                             agent=SimpleNamespace(id="critic"), services=SimpleNamespace(gateway=gateway))
    authorizations: list[str] = []

    class _Tools:
        def authorize(self, tool_id, inputs=None, *, bound=True):  # noqa: ANN001, ANN202
            authorizations.append(tool_id)
            if denied:
                raise PolicyDenied(f"tool {tool_id} denied: tool_denied_by_workspace_policy")

    monkeypatch.setattr(ctx_mod.RunContext, "check_control", lambda self: None)
    monkeypatch.setattr(ctx_mod.RunContext, "tools", lambda self: _Tools())
    count = {"n": used}

    class _S:
        def __enter__(self):  # noqa: ANN204
            return SimpleNamespace(scalar=lambda q: count["n"])

        def __exit__(self, *a):  # noqa: ANN002, ANN204
            return False

    monkeypatch.setattr(ctx_mod, "session_scope", lambda: _S())
    return ctx, runner, count, authorizations


def test_rerun_counts_toward_the_run_budget(monkeypatch):
    ctx, runner, count, _ = _ctx(monkeypatch, used=1, budget=2)
    run = ctx.run_sql("src_1")
    run("SELECT 1", purpose="verification.rerun", use_cache=False)
    assert runner.calls == [{"sql": "SELECT 1", "purpose": "verification.rerun", "max_rows": None, "use_cache": False}]
    count["n"] = 2
    with pytest.raises(BudgetExceeded, match="per-run query budget"):
        run("SELECT 1", purpose="verification.rerun", use_cache=False)
    assert len(runner.calls) == 1


# --------------------------------------------------------------------------- P4-C11 implicit gate
def test_run_sql_is_gated_as_sql_execute_once_per_step(monkeypatch):
    ctx, _, _, auths = _ctx(monkeypatch, used=0, budget=10)
    ctx.run_sql("a")
    ctx.run_sql("b")
    assert auths == ["sql.execute"]


def test_denied_sql_execute_blocks_run_sql_before_the_gateway(monkeypatch):
    ctx, runner, _, _ = _ctx(monkeypatch, used=0, budget=10, denied=True)
    with pytest.raises(PolicyDenied, match="sql.execute"):
        ctx.run_sql("a")
    assert runner.calls == []


# --------------------------------------------------------------------------- P4-C03 role naming
def test_workspace_reader_role_is_safe_prefixed_and_distinct():
    a, b = workspace_reader_role("ws_1f3a9c0b2d4e"), workspace_reader_role("ws_2f3a9c0b2d4e")
    assert a == "analystos_r_ws_1f3a9c0b2d4e" and a != b
    assert is_safe_identifier(a)
    assert workspace_reader_role("WS-Odd Name!", "aostest_r_") == "aostest_r_ws_odd_name"
    assert len(workspace_reader_role("ws_" + "x" * 80)) <= 63
