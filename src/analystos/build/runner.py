"""The customer's dbt runner (P4-E04): dbt Core as a separate process, never imported in-process.

The worker writes the approved project and a profile beside it, then runs the `dbt` executable
(its own virtualenv or container image, `ANALYSTOS_DBT_EXECUTABLE`) with a minimal environment: no
platform secret reaches it except the build identity's password, passed as an environment variable
the profile reads, so it is never written to disk or into the project hash. `parse` needs no
database connection; only `build` connects, as the build identity.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.engine import make_url

from analystos.build.project import PROFILE_NAME
from analystos.core.errors import InvalidInput, UpstreamUnavailable

PASSWORD_ENV = "ANALYSTOS_DBT_PASSWORD"
LOG_TAIL = 6000


@dataclass
class RunnerResult:
    command: str
    returncode: int
    log_tail: str
    manifest: dict[str, Any] = field(default_factory=dict)
    run_results: dict[str, Any] = field(default_factory=dict)
    osi_document: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Connection:
    """Where and as whom dbt connects: the build login, SET ROLE to the workspace build role."""

    host: str
    port: int
    dbname: str
    user: str
    password: str
    role: str
    schema: str

    @classmethod
    def from_url(cls, url: str, *, role: str, schema: str) -> Connection:
        u = make_url(url)
        return cls(host=u.host or "localhost", port=int(u.port or 5432), dbname=u.database or "", user=u.username or "",
                   password=u.password or "", role=role, schema=schema)


def write_profile(profiles_dir: Path, conn: Connection | None, *, schema: str) -> Path:
    """profiles.yml with the password as `env_var(...)`. A parse-only profile (conn=None) points nowhere."""
    output: dict[str, Any] = {"type": "postgres", "threads": 1, "schema": schema, "connect_timeout": 10,
                              "password": "{{ env_var('" + PASSWORD_ENV + "') }}"}
    if conn is None:
        output.update(host="127.0.0.1", port=1, user="parse_only", dbname="parse_only")
    else:
        output.update(host=conn.host, port=conn.port, user=conn.user, dbname=conn.dbname, role=conn.role)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / "profiles.yml"
    path.write_text(yaml.safe_dump({PROFILE_NAME: {"target": "build", "outputs": {"build": output}}}, sort_keys=True))
    return path


def write_project(project_dir: Path, files: dict[str, str]) -> None:
    if project_dir.exists():
        shutil.rmtree(project_dir)
    for rel, text in files.items():
        path = (project_dir / rel).resolve()
        if project_dir.resolve() not in path.parents:
            raise InvalidInput(f"project path {rel} escapes the job directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def read_project(project_dir: Path, paths: list[str]) -> dict[str, str]:
    return {rel: (project_dir / rel).read_text() for rel in paths}


class DbtCoreRunner:
    name = "dbt-core"

    def __init__(self, executable: str = "dbt", *, timeout_seconds: int = 1800) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def resolve(self) -> str | None:
        if os.path.sep in self.executable:
            return self.executable if os.access(self.executable, os.X_OK) else None
        return shutil.which(self.executable)

    def available(self) -> bool:
        return self.resolve() is not None

    def version(self) -> str | None:
        exe = self.resolve()
        if exe is None:
            return None
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60, env=_env(Path.home(), ""))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return next((ln.split(":", 1)[1].strip() for ln in out.stdout.splitlines() if "installed" in ln), None)

    def parse(self, job_dir: Path) -> RunnerResult:
        return self._run(job_dir, "parse", password="")

    def build(self, job_dir: Path, password: str) -> RunnerResult:
        return self._run(job_dir, "build", password=password)

    def _run(self, job_dir: Path, command: str, *, password: str) -> RunnerResult:
        exe = self.resolve()
        if exe is None:
            raise UpstreamUnavailable(f"the dbt runner '{self.executable}' is not installed (set ANALYSTOS_DBT_EXECUTABLE; "
                                      "install the `dbt` extra or a dbt Core image)")
        project, target = job_dir / "project", job_dir / "target"
        args = [exe, "--no-use-colors", command, "--project-dir", str(project), "--profiles-dir", str(job_dir / "profile"),
                "--target-path", str(target), "--log-path", str(job_dir / "logs")]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout_seconds, cwd=job_dir,
                                  env=_env(job_dir, password))
            code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            code, out = 124, f"dbt {command} timed out after {self.timeout_seconds}s\n{exc.stdout or ''}"
        if password:
            out = out.replace(password, "***")
        return RunnerResult(command=f"dbt {command}", returncode=code, log_tail=out[-LOG_TAIL:],
                            manifest=_json(target / "manifest.json"), run_results=_json(target / "run_results.json"),
                            osi_document=_json(target / "osi_document.json") or None)


def _env(home: Path, password: str) -> dict[str, str]:
    """Only what dbt needs: no platform secret (model keys, source passwords) reaches the runner."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8",
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "DBT_VERSION_CHECK": "false", "DBT_PARTIAL_PARSE": "false",
           PASSWORD_ENV: password}
    return env


def _json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
