"""P7-17 non-editable wheel: every repository file the code reads at run time (core/config.py DATA_ROOT) travels
inside the wheel under analystos/_data, and nothing outside the package is needed."""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

from analystos.capabilities import registry
from analystos.connectors import certification, kinds
from analystos.core import config
from analystos.core.config import Settings
from analystos.decisions import backends
from analystos.demo import seed

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "analystos/_data/"


def _hook():
    spec = importlib.util.spec_from_file_location("analystos_hatch_build", ROOT / "hatch_build.py")
    if importlib.util.find_spec("hatchling") is None:
        pytest.skip("hatchling is not installed (dev extra)")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_paths() -> list[Path]:
    """The data files and directories the code resolves below DATA_ROOT."""
    s = Settings(_env_file=None)
    return [s.models_config, s.oidc_mapping_file, s.agents_dir, kinds.CATALOG_PATH, backends.LOCAL_MODEL_PATH,
            seed.DEMO_FILE, seed.PROCESS_DEMO_FILE, registry.PACKS_DIR, certification.EVIDENCE_DIR,
            config.DATA_ROOT / "config" / "task_queues.yaml", config.DATA_ROOT / "alembic.ini",
            config.DATA_ROOT / "migrations" / "env.py", config.DATA_ROOT / "migrations" / "versions"]


def _required(names: set[str]) -> None:
    for path in _runtime_paths():
        rel = path.relative_to(config.DATA_ROOT).as_posix()
        if path.is_dir():
            assert any(n.startswith(f"{PREFIX}{rel}/") for n in names), f"{rel}/ is not bundled"
        else:
            assert f"{PREFIX}{rel}" in names, f"{rel} is not bundled"
    versions = {p.name for p in (ROOT / "migrations/versions").glob("*.py")}
    assert {n.rsplit("/", 1)[1] for n in names if n.startswith(f"{PREFIX}migrations/versions/")} == versions
    assert any(n.startswith(f"{PREFIX}docs/60-delivery/evidence/connector-") for n in names)


def test_a_checkout_reads_data_from_the_repository_and_writes_state_there():
    assert config.SOURCE_CHECKOUT
    assert config.DATA_ROOT == config.STATE_ROOT == ROOT
    for path in _runtime_paths():
        assert path.exists(), path


def test_the_build_hook_bundles_every_runtime_file():
    hook = _hook()
    bundled = hook.bundled_files(ROOT)
    names = set(bundled.values())
    _required(names)
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    # only live connector evidence travels from docs/, not the whole evidence folder
    assert all(Path(n).name.startswith("connector-") for n in names if "/docs/" in n)


@pytest.mark.slow
def test_the_built_wheel_contains_the_runtime_data(tmp_path):
    _hook()
    from hatchling.builders.wheel import WheelBuilder

    wheel = next(iter(WheelBuilder(str(ROOT)).build(directory=str(tmp_path), versions=["standard"])))
    names = set(zipfile.ZipFile(wheel).namelist())
    _required(names)
    assert "analystos/cli.py" in names and "analystos/api/app.py" in names
    for playbook in (ROOT / "src/analystos/capabilities/builtin/playbooks").glob("*.yaml"):
        assert f"analystos/capabilities/builtin/playbooks/{playbook.name}" in names
    assert not [n for n in names if "__pycache__" in n]
    assert not [n for n in names if n.startswith(("tests/", "docs/", "config/", "web/"))]  # data only under _data


def test_the_image_installs_the_wheel_built_from_every_bundled_source():
    dockerfile = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    copied = {p for line in dockerfile.splitlines() if line.startswith("COPY ") and "--from" not in line
              for p in line.split()[1:-1]}
    assert {"hatch_build.py", "alembic.ini", "src", "config", "migrations", "packs", "docs/60-delivery/evidence"} <= copied
    assert "pip wheel" in dockerfile and " -e " not in dockerfile  # non-editable: byte-compiled at install
