"""Build the hosted run viewer and deploy it as a Cloudflare static-assets Worker.

The site holds every committed run under ``runs/`` plus the scratch runs listed
in ``runs-local/published.json``. Pages are rendered into a staging directory;
run directories are only read. The site is unlisted rather than gated: every
page sits under a random path segment, the bare hostname answers 404, and
responses ask search engines not to index them.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..runner import _RunMetadata, _base_runs_root
from . import generate
from .runs_index import generate_index

DEFAULT_HOST = "simmons.stage11.dev"
DEFAULT_WORKER = "simmons-viewer"
TOKEN_ENV = "SIMMONS_CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
PATH_ENV = "SIMMONS_PUBLISH_PATH"
LIST_NAME = "published.json"
STAGING_NAME = ".publish"
# cf builds static-assets Workers by delegating to Wrangler, so the deploy
# project declares both. Exact pins: cf is in beta.
CF_VERSION = "1.0.0-beta.14"
WRANGLER_VERSION = "4.149.0"
INSTALL = ("npm", "install", "--no-audit", "--no-fund", "--loglevel=error")
CF = ("npx", "--no-install", "cf")

_HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
  Cache-Control: no-store
  Referrer-Policy: no-referrer
"""


class PublishError(RuntimeError):
    """A publish step failed; the message says which and why."""


@dataclass(frozen=True)
class Target:
    host: str
    worker: str
    path: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.path}/"


@dataclass
class PublishState:
    """The unlisted path and the scratch runs chosen for publishing."""

    path: str
    runs: list[str]
    saved: bool = True


@dataclass
class SiteBuild:
    site: Path
    index: Path
    runs: list[dict[str, Any]]
    skipped: list[str] = field(default_factory=list)


def roots(repo_root: Path) -> tuple[Path, Path]:
    """Return the committed and scratch run roots."""
    return _base_runs_root(repo_root, False), _base_runs_root(repo_root, True)


def state_path(repo_root: Path) -> Path:
    return _base_runs_root(repo_root, True) / LIST_NAME


def new_path() -> str:
    return secrets.token_hex(8)


def read_state(repo_root: Path) -> PublishState:
    """Read ``runs-local/published.json``; a missing file starts a new unlisted path."""
    path = state_path(repo_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PublishState(path=new_path(), runs=[], saved=False)
    except ValueError as exc:
        raise PublishError(f"{path}: not valid JSON ({exc})") from exc
    runs = value.get("runs") if isinstance(value, Mapping) else None
    slug = value.get("path") if isinstance(value, Mapping) else None
    if not isinstance(runs, list) or not all(isinstance(item, str) for item in runs):
        raise PublishError(f'{path}: expected {{"path": "...", "runs": ["<experiment>/<run-id>", ...]}}')
    saved = isinstance(slug, str) and slug.isalnum()
    return PublishState(path=slug if saved else new_path(), runs=list(dict.fromkeys(runs)), saved=saved)


def write_state(repo_root: Path, state: PublishState) -> Path:
    path = state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"path": state.path, "runs": sorted(dict.fromkeys(state.runs))}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    state.saved = True
    return path


def target_from_env(state: PublishState, env: Mapping[str, str] | None = None) -> Target:
    env = os.environ if env is None else env
    return Target(
        host=env.get("SIMMONS_PUBLISH_HOST") or DEFAULT_HOST,
        worker=env.get("SIMMONS_PUBLISH_WORKER") or DEFAULT_WORKER,
        path=env.get(PATH_ENV) or state.path,
    )


def scratch_key(repo_root: Path, run_dir: Path) -> str | None:
    """Return a run's key under ``runs-local/``, or None for a committed run.

    Raises when the directory is not a run or lies outside both run roots.
    """
    resolved = run_dir.resolve()
    if not (resolved / "run.json").is_file():
        raise PublishError(f"{run_dir}: no run.json; pass a run directory")
    committed, scratch = (root.resolve() for root in roots(repo_root))
    if resolved.is_relative_to(committed):
        return None
    if resolved.is_relative_to(scratch):
        return resolved.relative_to(scratch).as_posix()
    raise PublishError(f"{run_dir}: not under {committed} or {scratch}")


def build_site(repo_root: Path, scratch_runs: Sequence[str], path: str) -> SiteBuild:
    """Render the published runs and their index into ``runs-local/.publish/site/<path>``."""
    committed, scratch = (root.resolve() for root in roots(repo_root))
    site = scratch / STAGING_NAME / "site"
    if site.exists():
        shutil.rmtree(site)
    pages = site / path
    pages.mkdir(parents=True)
    chosen = {(scratch / key).resolve() for key in scratch_runs}
    present = {run for run in chosen if (run / "run.json").is_file()}
    skipped = [
        f"runs-local/{key}: listed but not found on this machine"
        for key in scratch_runs
        if (scratch / key).resolve() not in present
    ]

    def include(item: _RunMetadata) -> bool:
        run = item.path.resolve()
        return run.is_relative_to(committed) or run in present

    def view_for(item: _RunMetadata, status: str) -> tuple[Path | None, str | None]:
        run = item.path.resolve()
        root = committed if run.is_relative_to(committed) else scratch
        label = f"{root.name}/{run.relative_to(root).as_posix()}"
        if status == "running":
            skipped.append(f"{label}: still running")
            return None, "Run in progress; publish again when it finishes."
        output = pages / root.name / run.relative_to(root)
        try:
            index, _ = generate(run, output_dir=output)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            skipped.append(f"{label}: view failed ({exc})")
            return None, f"View could not be generated: {exc}"
        return index, None

    index, data = generate_index((committed, scratch), pages / "index.html", include=include, view_for=view_for)
    (site / "_headers").write_text(_HEADERS, encoding="utf-8")
    return SiteBuild(site=site, index=index, runs=list(data["runs"]), skipped=skipped)


def cf_config(target: Target) -> dict[str, Any]:
    """Static assets only, on the custom domain; no workers.dev or preview hostnames."""
    return {
        "worker": {
            "name": target.worker,
            "compatibilityDate": "2026-10-01",
            "assets": {"htmlHandling": "auto-trailing-slash", "notFoundHandling": "none"},
            "workersDev": False,
            "previewUrls": False,
            "domains": [target.host],
        }
    }


def write_project(target: Target, site: Path) -> Path:
    """Write the cf deploy project beside the site and return its directory.

    ``defineConfig`` only adds types, so the configs are plain default exports
    and need no import resolution. The assets directory belongs to the Wrangler
    build config, not ``cloudflare.config.ts``.
    """
    project = site.parent
    package = {
        "name": "simmons-viewer-deploy",
        "private": True,
        "type": "module",
        "devDependencies": {"cf": CF_VERSION, "wrangler": WRANGLER_VERSION},
    }
    files = {
        "package.json": json.dumps(package, indent=2) + "\n",
        "cloudflare.config.ts": f"export default {json.dumps(cf_config(target), indent=2)};\n",
        "wrangler.config.ts": f"export default {json.dumps({'assetsDirectory': site.name})};\n",
    }
    for name, text in files.items():
        (project / name).write_text(text, encoding="utf-8")
    (project / "wrangler.json").unlink(missing_ok=True)  # left by Wrangler-era publishes
    return project


Runner = Callable[..., subprocess.CompletedProcess[str]]


def deploy(
    build: SiteBuild,
    target: Target,
    env: Mapping[str, str] | None = None,
    run: Runner = subprocess.run,
) -> None:
    env = dict(os.environ if env is None else env)
    token = env.get(TOKEN_ENV)
    account = env.get(ACCOUNT_ENV)
    missing = [name for name, value in ((TOKEN_ENV, token), (ACCOUNT_ENV, account)) if not value]
    if missing:
        raise PublishError(
            f"set {' and '.join(missing)} in the environment or the repo's .env "
            "(see docs/hosted-viewer.md)"
        )
    project = write_project(target, build.site)
    child_env = {
        **env,
        "CLOUDFLARE_API_TOKEN": str(token),
        "CLOUDFLARE_ACCOUNT_ID": str(account),
        "CF_SEND_TELEMETRY": "false",
        "WRANGLER_SEND_METRICS": "false",
    }
    for step, argv in (("npm install", INSTALL), ("cf deploy", (*CF, "deploy"))):
        try:
            result = run(list(argv), cwd=project, env=child_env, text=True)
        except FileNotFoundError as exc:
            raise PublishError(f"{argv[0]} not found; install Node.js to deploy") from exc
        if result.returncode != 0:
            raise PublishError(f"{step} exited {result.returncode}")


def _status(url: str, timeout: float) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "simmons-publish"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def check_live(
    target: Target,
    timeout: float = 20.0,
    wait: float = 30.0,
    probe: Callable[[str, float], int] = _status,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Return the index's HTTP status, or None if the host stays unreachable for ``wait`` seconds."""
    deadline = time.monotonic() + wait
    while True:
        try:
            return probe(target.url, timeout)
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                return None
            sleep(5.0)


__all__ = [
    "PublishError",
    "PublishState",
    "SiteBuild",
    "Target",
    "build_site",
    "cf_config",
    "check_live",
    "deploy",
    "read_state",
    "scratch_key",
    "target_from_env",
    "write_project",
    "write_state",
]
