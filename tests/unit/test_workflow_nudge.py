"""AnalysisWorkflow keeps a `nudge` that arrives while it reads the run state (live 2026-09-27: an approval sent right
after a resume landed during get_state, the wait then cleared it and slept its 300 s re-check, so the publication
started five minutes late). Driven with a stand-in for `temporalio.workflow`; no Temporal server needed."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from analystos.workflows import analysis_workflow as aw

OPTS = {"resumed": True, "queues": {"analysis": {"name": "t9-analysis", "start_to_close": 60, "heartbeat": 30}}}


class FakeWorkflowModule:
    """Only what AnalysisWorkflow.run touches. get_state answers from `states`; `during_state` runs inside it."""

    logger = logging.getLogger("fake-workflow")

    def __init__(self, wf: aw.AnalysisWorkflow, states: list[dict], *, patched: bool, during_state=None):
        self.wf, self.states, self.is_patched, self.during_state = wf, list(states), patched, during_state
        self.waits: list[tuple[bool, float]] = []  # (condition already true, timeout seconds)
        self.calls: list[str] = []

    def info(self):
        return SimpleNamespace(task_queue="t9-analysis", is_continue_as_new_suggested=lambda: False)

    def patched(self, patch_id: str) -> bool:
        assert patch_id == aw.NUDGE_BEFORE_STATE
        return self.is_patched

    async def execute_activity(self, name, *args, **kwargs):
        self.calls.append(name)
        if name == "get_state":
            state = self.states.pop(0)
            if self.during_state and len(self.calls) == 1:
                self.during_state()
            return state
        return None

    async def wait_condition(self, fn, timeout):
        self.waits.append((bool(fn()), timeout.total_seconds()))
        if not fn():
            raise TimeoutError


def _run(monkeypatch, *, patched: bool) -> FakeWorkflowModule:
    wf = aw.AnalysisWorkflow()
    fake = FakeWorkflowModule(wf, [{"waiting_user": True}, {"done": True}], patched=patched, during_state=wf.nudge)
    monkeypatch.setattr(aw, "workflow", fake)
    assert asyncio.run(wf.run("run_1", OPTS)) == "COMPLETED"
    return fake


def test_a_nudge_during_the_state_read_wakes_the_wait_at_once(monkeypatch):
    fake = _run(monkeypatch, patched=True)
    assert fake.waits == [(True, 300.0)]  # the approval's nudge was kept: no 300 s sleep
    assert fake.calls == ["get_state", "get_state", "finish_run"]


def test_histories_from_before_the_fix_replay_as_they_ran(monkeypatch):
    fake = _run(monkeypatch, patched=False)
    assert fake.waits == [(False, 300.0)]  # the old behaviour: the nudge was cleared and the wait timed out


@pytest.mark.parametrize("patched", [True, False])
def test_a_nudge_during_the_wait_still_wakes_it(monkeypatch, patched):
    wf = aw.AnalysisWorkflow()
    fake = FakeWorkflowModule(wf, [{"waiting_user": True}, {"done": True}], patched=patched)

    async def wait_condition(fn, timeout):
        wf.nudge()  # the signal arrives while the workflow is parked
        fake.waits.append((bool(fn()), timeout.total_seconds()))

    fake.wait_condition = wait_condition
    monkeypatch.setattr(aw, "workflow", fake)
    assert asyncio.run(wf.run("run_1", OPTS)) == "COMPLETED"
    assert fake.waits == [(True, 300.0)]
