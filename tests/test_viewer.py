from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import simmons.config as model_config
import simmons.viewer as viewer
from simmons.commands.view import _run
from simmons.records import canonical_json
from simmons.viewer import _load_run_data, generate, render_episode_page, render_run_page

FIXTURES = Path(__file__).parent / "fixtures" / "runs"
BASE_RUN = FIXTURES / "base" / "sender-a__guard-a"
WATCHLIST_RUN = FIXTURES / "watchlist" / "all-kinds"
HOSTILE = "</script><script>alert(1)</script>"


def _copy_run(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _page_data(path: Path) -> dict[str, object]:
    page = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="viewer-data" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_viewer_writes_run_page_and_one_page_per_episode(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")

    index, episodes = generate(run)

    assert index == run / "view" / "index.html"
    assert len(episodes) == 20
    assert (run / "view" / "episode-000000.html").is_file()
    assert (run / "view" / "episode-000019.html").is_file()
    data = _page_data(index)
    assert len(data["episodes"]) == 20
    assert data["summary"]["curve"]["points"]
    page = index.read_text(encoding="utf-8")
    assert "All outcomes" in page
    assert "Catch vs false-alarm curve" in page


def test_run_page_has_effective_models_and_guard_guess_correctness(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    episode_path = run / "episodes.jsonl"
    episodes = [json.loads(line) for line in episode_path.read_text(encoding="utf-8").splitlines()]
    episodes[0]["guard"]["guess"] = dict(episodes[0]["target"])
    episode_path.write_text("".join(canonical_json(row) + "\n" for row in episodes), encoding="utf-8")

    index, _ = generate(run)

    data = _page_data(index)
    assert set(data["models"]) == {"sender", "receiver", "guard", "plain_sender", "judge"}
    assert data["models"]["sender"] == {"provider": "fake", "model": "covert"}
    assert data["models"]["judge"] is None
    assert data["episodes"][0]["guess_correct"] is True
    assert any(row["guess_correct"] is False for row in data["episodes"][1:])
    page = index.read_text(encoding="utf-8")
    for label in ("Sender model", "Receiver model", "Guard model", "Plain sender model", "Judge model"):
        assert label in page
    assert " ✓" in page


def test_run_page_uses_models_recorded_before_alias_retarget(
    tmp_path: Path, monkeypatch
) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    config_path = run / "config.json"
    recorded_config = json.loads(config_path.read_text(encoding="utf-8"))
    recorded_config["models"] = "historical-alias"
    config_path.write_text(json.dumps(recorded_config), encoding="utf-8")
    monkeypatch.setattr(
        model_config,
        "_load_aliases",
        lambda: {"historical-alias": "fake:retargeted"},
    )

    run_data, _ = _load_run_data(run)

    assert run_data["models"]["sender"] == {"provider": "fake", "model": "covert"}
    assert run_data["models"]["guard"] == {"provider": "fake", "model": "covert"}
    assert run_data["models"]["judge"] is None


def test_episode_page_shows_matching_guard_request_and_catch_receiver_state(
    tmp_path: Path,
) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    generate(run)

    path = run / "view" / "episode-000003.html"
    page = path.read_text(encoding="utf-8")
    data = _page_data(path)
    guard = next(role for role in data["roles"] if role["slot"] == "guard")
    guard_user = "\n".join(part.get("text", "") for part in guard["request"]["user"])

    assert data["episode"]["outcome"] == "CATCH"
    assert guard["hash_match"] is True
    assert all(role["hash_match"] for role in data["roles"] if role["called"])
    assert "<target>" not in guard_user
    assert data["target_index"] == data["episode"]["target"]["index"]
    assert "Not called: the guard blocked the message." in page


def test_request_replay_uses_the_run_tool_snapshot(tmp_path: Path, monkeypatch) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    current_request_for = viewer.request_for

    def changed_request_for(*args, **kwargs):
        request = current_request_for(*args, **kwargs)
        changed_tool = replace(request.tool, description="Changed after this run.")
        changed_prior = request.prior
        if changed_prior is not None:
            changed_prior = replace(
                changed_prior,
                tool=replace(changed_prior.tool, description="Changed send tool."),
            )
        return replace(request, tool=changed_tool, prior=changed_prior)

    monkeypatch.setattr(viewer, "request_for", changed_request_for)
    _, pages = _load_run_data(run)

    assert all(
        role["hash_match"]
        for page in pages
        for role in page["roles"]
        if role["called"]
    )


def test_snapshot_option_enums_follow_each_episode_plan() -> None:
    run = BASE_RUN
    roles = json.loads((run / "inputs" / "tools.json").read_text(encoding="utf-8"))["roles"]
    plan_rows = [
        json.loads(line)
        for line in (run / "plan.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    saved_options = plan_rows[0]["options"]
    episode_options = [*saved_options[1:], saved_options[0]]

    guard_tool = viewer._snapshot_tool(roles, "guard", saved_options, episode_options)
    receiver_tool = viewer._snapshot_tool(roles, "receiver", saved_options, episode_options)

    assert guard_tool is not None
    assert receiver_tool is not None
    assert guard_tool.input_schema["properties"]["guess"]["enum"] == [
        *episode_options,
        None,
    ]
    assert receiver_tool.input_schema["properties"]["pick"]["enum"] == episode_options
    assert guard_tool.description == roles["guard"]["description"]


def test_watchlist_episode_marks_target_and_protected_options(tmp_path: Path) -> None:
    run = _copy_run(WATCHLIST_RUN, tmp_path / "run")
    generate(run)

    data = _page_data(run / "view" / "episode-000000.html")
    target_index = data["target_index"]

    assert data["protected_indices"]
    assert target_index in data["protected_indices"]
    assert "target" in (run / "view" / "episode-000000.html").read_text(encoding="utf-8")
    assert "protected" in (run / "view" / "episode-000000.html").read_text(encoding="utf-8")


def test_hostile_option_is_embedded_as_text_not_markup(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    plan_path = run / "plan.jsonl"
    rows = [json.loads(line) for line in plan_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["options"][0] = HOSTILE
    plan_path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")

    generate(run)

    path = run / "view" / "episode-000000.html"
    page = path.read_text(encoding="utf-8")
    data = _page_data(path)
    assert data["options"][0] == HOSTILE
    assert HOSTILE not in page
    assert r"\u003c/script\u003e\u003cscript\u003ealert(1)\u003c/script\u003e" in page
    assert "innerHTML" not in page
    assert not re.search(
        r'<(?:script|link|img|iframe)\b[^>]+(?:src|href)=["\']https?://', page, re.I
    )


def test_hostile_strings_in_every_page_data_field_are_html_safe(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    run_data, episode_data = _load_run_data(run)

    def poison(value: object, key: str | None = None) -> object:
        if isinstance(value, dict):
            return {name: poison(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [poison(item) for item in value]
        if isinstance(value, str) and key not in {"page", "href"}:
            return HOSTILE
        return value

    run_page = render_run_page(poison(run_data))
    episode_page = render_episode_page(poison(episode_data[0]))
    escaped = r"\u003c/script\u003e\u003cscript\u003ealert(1)\u003c/script\u003e"

    for page in (run_page, episode_page):
        assert HOSTILE not in page
        assert escaped in page
        assert "innerHTML" not in page
        assert not re.search(
            r'<(?:script|link|img|iframe)\b[^>]+(?:src|href)=["\']https?://', page, re.I
        )
        assert page.count('<script') == 2
        for api in ("fetch(", "XMLHttpRequest", "WebSocket(", "EventSource(", "sendBeacon("):
            assert api not in page


def test_open_flag_uses_only_the_generated_local_file_url(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    opened: list[str] = []
    monkeypatch.setattr(
        "simmons.commands.view.webbrowser.open",
        lambda url: opened.append(url) or True,
    )

    result = _run(type("Args", (), {"run_dir": run, "open": True})())

    assert result == 0
    assert len(opened) == 1
    assert opened[0].startswith("file://")
    assert capsys.readouterr().out.count("episode pages") == 1
