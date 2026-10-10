from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from simmons.commands.publish import _run
from simmons.viewer import publish

FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def _page_data(path: Path) -> dict:
    match = re.search(
        r'<script id="viewer-data" type="application/json">(.*?)</script>',
        path.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _stage(tmp_path: Path, monkeypatch) -> Path:
    shutil.copytree(FIXTURES / "base" / "sender-a__guard-a", tmp_path / "runs" / "base" / "sender-a__guard-a")
    for name in ("sender-b__guard-a", "sender-c__guard-b"):
        shutil.copytree(FIXTURES / "base" / name, tmp_path / "runs-local" / "base" / name)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    return tmp_path


def _args(*run_dirs: Path, remove: tuple[Path, ...] = (), build_only: bool = True) -> argparse.Namespace:
    return argparse.Namespace(run_dirs=list(run_dirs), remove=list(remove), build_only=build_only)


def _state(root: Path) -> dict:
    return json.loads((root / "runs-local" / "published.json").read_text(encoding="utf-8"))


def _site_runs(root: Path) -> dict[str, dict]:
    data = _page_data(root / "runs-local" / ".publish" / "site" / _state(root)["path"] / "index.html")
    return {run["run_id"]: run for run in data["runs"]}


def test_publish_builds_committed_and_listed_runs_without_touching_run_dirs(tmp_path, monkeypatch) -> None:
    root = _stage(tmp_path, monkeypatch)
    chosen = root / "runs-local" / "base" / "sender-b__guard-a"

    assert _run(_args(chosen)) == 0

    state = _state(root)
    assert state["runs"] == ["base/sender-b__guard-a"]
    assert re.fullmatch(r"[0-9a-f]{16}", state["path"])
    site = root / "runs-local" / ".publish" / "site"
    assert {p.name for p in site.iterdir()} == {"_headers", state["path"]}
    runs = _site_runs(root)
    assert len(runs) == 2
    assert {run["root"] for run in runs.values()} == {"runs", "runs-local"}
    for run in runs.values():
        page = site / state["path"] / run["href"]
        assert page.is_file() and page.resolve().is_relative_to(site.resolve())
        episodes = _page_data(page)["episodes"]
        assert episodes and all((page.parent / item["href"]).is_file() for item in episodes)
    assert (site / "_headers").read_text(encoding="utf-8").startswith("/*\n")
    assert not list(root.glob("runs*/base/*/view"))


def test_publish_list_persists_and_remove_accepts_a_missing_run(tmp_path, monkeypatch, capsys) -> None:
    root = _stage(tmp_path, monkeypatch)
    b = root / "runs-local" / "base" / "sender-b__guard-a"
    c = root / "runs-local" / "base" / "sender-c__guard-b"
    assert _run(_args(b, c)) == 0
    assert len(_site_runs(root)) == 3

    path = _state(root)["path"]
    shutil.rmtree(c)
    assert _run(_args()) == 0
    assert _state(root)["path"] == path
    assert "listed but not found" in capsys.readouterr().err
    assert len(_site_runs(root)) == 2

    assert _run(_args(remove=(Path("runs-local/base/sender-c__guard-b"),))) == 0
    assert _state(root) == {"path": path, "runs": ["base/sender-b__guard-a"]}

    assert _run(_args(remove=(b,))) == 0
    assert len(_site_runs(root)) == 1


def test_publish_refuses_a_directory_outside_the_run_roots(tmp_path, monkeypatch, capsys) -> None:
    root = _stage(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere" / "run"
    shutil.copytree(FIXTURES / "base" / "sender-a__guard-a", outside)

    assert _run(_args(outside)) == 1
    assert "not under" in capsys.readouterr().err
    assert not (root / "runs-local" / "published.json").exists()


def test_cf_config_serves_only_the_custom_domain() -> None:
    worker = publish.cf_config(publish.Target("simmons.stage11.dev", "simmons-viewer", "abc123"))["worker"]

    assert worker["workersDev"] is False
    assert worker["previewUrls"] is False
    assert worker["domains"] == ["simmons.stage11.dev"]
    assert worker["assets"]["notFoundHandling"] == "none"
    assert "entrypoint" not in worker


def test_deploy_installs_then_runs_cf_with_the_project_token(tmp_path) -> None:
    site = tmp_path / ".publish" / "site"
    site.mkdir(parents=True)
    build = publish.SiteBuild(site=site, index=site / "index.html", runs=[])
    target = publish.Target("simmons.stage11.dev", "simmons-viewer", "abc123")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(publish.PublishError, match="SIMMONS_CLOUDFLARE_API_TOKEN"):
        publish.deploy(build, target, env={"CLOUDFLARE_ACCOUNT_ID": "acct"}, run=run)
    assert calls == []

    env = {"SIMMONS_CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_API_TOKEN": "other"}
    (site.parent / "wrangler.json").write_text("{}", encoding="utf-8")
    publish.deploy(build, target, env=env, run=run)
    assert [argv[:2] for argv, _ in calls] == [["npm", "install"], ["npx", "--no-install"]]
    argv, kwargs = calls[1]
    assert argv[-2:] == ["cf", "deploy"]
    assert kwargs["cwd"] == site.parent
    assert kwargs["env"]["CLOUDFLARE_API_TOKEN"] == "tok"
    assert kwargs["env"]["CLOUDFLARE_ACCOUNT_ID"] == "acct"
    assert kwargs["env"]["CF_SEND_TELEMETRY"] == "false"
    package = json.loads((site.parent / "package.json").read_text(encoding="utf-8"))
    pins = {"cf": publish.CF_VERSION, "wrangler": publish.WRANGLER_VERSION}
    assert package["devDependencies"] == pins
    config = (site.parent / "cloudflare.config.ts").read_text(encoding="utf-8")
    assert config.startswith("export default ")
    worker = json.loads(config.removeprefix("export default ").rstrip().rstrip(";"))["worker"]
    assert worker["name"] == "simmons-viewer"
    build_config = (site.parent / "wrangler.config.ts").read_text(encoding="utf-8")
    assert '"assetsDirectory": "site"' in build_config
    assert not (site.parent / "wrangler.json").exists()

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)

    with pytest.raises(publish.PublishError, match="npm install exited 1"):
        publish.deploy(build, target, env=env, run=failing)


def test_path_override_and_url() -> None:
    state = publish.PublishState(path="abc123", runs=[])
    assert publish.target_from_env(state, env={}).url == "https://simmons.stage11.dev/abc123/"
    assert publish.target_from_env(state, env={"SIMMONS_PUBLISH_PATH": "zz9"}).path == "zz9"


def test_live_check_retries_a_new_hostname_then_reports_unreachable() -> None:
    target = publish.Target("simmons.stage11.dev", "simmons-viewer", "abc123")
    answers: list = [urllib.error.URLError("no such host"), 200]
    sleeps: list[float] = []

    def probe(url, timeout):
        assert url == "https://simmons.stage11.dev/abc123/"
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert publish.check_live(target, probe=probe, sleep=sleeps.append) == 200
    assert sleeps == [5.0]

    def down(url, timeout):
        raise urllib.error.URLError("no such host")

    assert publish.check_live(target, wait=0.0, probe=down, sleep=sleeps.append) is None


def test_a_list_without_a_path_gets_one_that_then_stays(tmp_path, monkeypatch) -> None:
    root = _stage(tmp_path, monkeypatch)
    state_file = root / "runs-local" / "published.json"
    state_file.write_text(json.dumps({"runs": ["base/sender-b__guard-a"]}), encoding="utf-8")

    assert _run(_args()) == 0
    first = _state(root)
    assert first["runs"] == ["base/sender-b__guard-a"]
    assert re.fullmatch(r"[0-9a-f]{16}", first["path"])

    assert _run(_args()) == 0
    assert _state(root) == first
