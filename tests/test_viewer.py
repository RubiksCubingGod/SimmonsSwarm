from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from html.parser import HTMLParser
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
_HTML_PARSING_SINK = re.compile(
    r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|DOMParser|"
    r"createContextualFragment|setHTML|setHTMLUnsafe)\b|"
    r"\bdocument\s*\.\s*write\b"
)
_SAFE_TEXT_INSERTION = re.compile(
    r"\b[\w$.]+\s*\.\s*textContent\s*=|"
    r"\b(?:document\s*\.\s*)?createTextNode\s*\("
)
_URL_ATTRIBUTES = {
    "href",
    "src",
    "srcset",
    "imagesrcset",
    "action",
    "formaction",
    "poster",
    "data",
    "xlink:href",
}
_CSS_EXTERNAL_REFERENCE = re.compile(
    r"(?:\burl\s*\(\s*['\"]?\s*|"
    r"@import\b\s+(?:url\s*\(\s*['\"]?\s*)?['\"]?\s*)"
    r"(?:[a-z][a-z0-9+.-]*:|//)",
    re.IGNORECASE,
)
_EXTERNAL_URL_ATTRIBUTE = re.compile(
    r"(?:^|[\s,])(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE
)


class _PageSources(HTMLParser):
    """Collect CSS and URL-bearing attributes from generated static markup."""

    def __init__(self) -> None:
        super().__init__()
        self.css: list[str] = []
        self.url_attributes: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "style":
            self._in_style = True
        for name, value in attrs:
            if name.casefold() == "style":
                self.css.append(value or "")
            if name.casefold() in _URL_ATTRIBUTES:
                self.url_attributes.append(value or "")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.css.append(data)


def _external_css_references(page: str) -> list[str]:
    sources = _PageSources()
    sources.feed(page)
    references: list[str] = []
    for css in sources.css:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        references.extend(match.group(0) for match in _CSS_EXTERNAL_REFERENCE.finditer(css))
    return references


def _assert_no_external_urls(page: str) -> None:
    sources = _PageSources()
    sources.feed(page)
    for value in sources.url_attributes:
        assert _EXTERNAL_URL_ATTRIBUTE.search(value) is None, f"external URL attribute: {value}"
    references = _external_css_references(page)
    assert not references, f"external CSS URL reference: {references[0]}"


def _assert_text_only_dom_insertion(page: str) -> None:
    scripts = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
    assert len(scripts) == 1
    script = scripts[0]
    sink = _HTML_PARSING_SINK.search(script)
    assert sink is None, (
        "HTML-parsing DOM sink in generated JavaScript: "
        f"{sink.group(0) if sink else ''}"
    )
    assert _SAFE_TEXT_INSERTION.search(script), (
        "generated JavaScript has no textContent/createTextNode insertion"
    )
    assert re.search(
        r"const el=\(tag,text=\"\",cls=\"\"\)=>\{[^}]*\bn\.textContent=String\(text\)",
        script,
    ), "the element helper must render text through textContent"
    assert re.search(
        r"const add=\(parent,tag,text=\"\",cls=\"\"\)=>\{const n=el\(tag,text,cls\);",
        script,
    ), "the add helper must route text through the safe element helper"


def _copy_run(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _page_data_from_html(page: str) -> dict[str, object]:
    match = re.search(
        r'<script id="viewer-data" type="application/json">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _page_data(path: Path) -> dict[str, object]:
    return _page_data_from_html(path.read_text(encoding="utf-8"))


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


def test_run_page_labels_configured_model_when_role_was_not_called(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    run_data, _ = _load_run_data(run)
    models = viewer._recorded_models(
        {
            "preflight": {
                "roles": [
                    {"role": "guard", "model": "fake:guard"},
                    {"role": "receiver", "model": "fake:receiver"},
                ]
            }
        },
        [{"role": "sender", "model": {"provider": "fake", "model": "covert"}}],
        {
            "models": {
                "sender": "fake:covert",
                "default": "historical-alias",
                "judge": "historical-judge",
            }
        },
    )
    run_data["models"] = models

    page = render_run_page(run_data)

    assert models["plain_sender"] == {"configured": "historical-alias"}
    assert models["judge"] == {"configured": "historical-judge"}
    assert _page_data_from_html(page)["models"]["plain_sender"] == {
        "configured": "historical-alias"
    }
    assert _page_data_from_html(page)["models"]["judge"] == {
        "configured": "historical-judge"
    }
    assert "configured; no call recorded" in page


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


def test_episode_labels_use_recorded_indices_everywhere(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    generate(run)

    run_page = (run / "view" / "index.html").read_text(encoding="utf-8")
    episode_path = run / "view" / "episode-000003.html"
    episode_page = episode_path.read_text(encoding="utf-8")
    episode_data = _page_data(episode_path)

    assert episode_data["episode"]["index"] == 3
    assert "#${item.index}" in run_page
    assert "#${item.index+1}" not in run_page
    assert "<title>Episode 3 · Simmons episode viewer</title>" in episode_page
    assert "Episode ${Number(e.index)}" in episode_page
    assert "Episode ${Number(e.index)+1}" not in episode_page
    assert "#${item.episode} · ${item.outcome}" in episode_page
    assert "#${item.episode+1} · ${item.outcome}" not in episode_page
    assert episode_data["attempt_log"]
    assert episode_data["attempt_log"][0]["episode"] == 0


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
    _assert_text_only_dom_insertion(page)
    _assert_no_external_urls(page)


def test_css_external_url_scanner_covers_style_blocks_and_attributes() -> None:
    examples = (
        '<style>@import url("https://example.invalid/viewer.css");</style>',
        '<style>@import "//example.invalid/viewer.css";</style>',
        '<style>.remote{background:url(https://example.invalid/image.svg)}</style>',
        '<div style="background-image: url(data:image/svg+xml,abc)"></div>',
        '<div style="background-image: url(//example.invalid/image.svg)"></div>',
    )

    for markup in examples:
        assert _external_css_references(markup), markup


def test_generated_pages_have_no_external_urls_in_markup_or_css(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    run_data, episode_data = _load_run_data(run)

    for page in (render_run_page(run_data), render_episode_page(episode_data[0])):
        _assert_no_external_urls(page)


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
        _assert_text_only_dom_insertion(page)
        _assert_no_external_urls(page)
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


def test_run_page_lists_snapshot_prompts_by_recipient(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")

    index, _ = generate(run)

    prompts = {item["label"]: item for item in _page_data(index)["prompts"]}
    inputs = run / "inputs"
    assert prompts["P₁ · sender prompt"]["text"] == (inputs / "prompts" / "sender.md").read_text(encoding="utf-8")
    assert prompts["P₂ · guard prompt"]["text"] == (inputs / "prompts" / "guard.md").read_text(encoding="utf-8")
    assert prompts["Cover task"]["text"] == (inputs / "cover-task.md").read_text(encoding="utf-8")
    assert "sender and receiver" in prompts["P₁ · sender prompt"]["who"]
    assert [label for label, item in prompts.items() if item["open"]] == ["P₁ · sender prompt", "P₂ · guard prompt"]
    for path in sorted((inputs / "templates").glob("*.md")):
        assert any(item["source"] == f"inputs/templates/{path.name}" for item in prompts.values())


def test_episode_roles_carry_plain_english_explanations(tmp_path: Path) -> None:
    run = _copy_run(BASE_RUN, tmp_path / "run")
    generate(run)

    path = run / "view" / "episode-000003.html"
    roles = {role["label"]: role for role in _page_data(path)["roles"]}
    page = path.read_text(encoding="utf-8")

    assert all(role["about"] for role in roles.values())
    assert "cannot change the outcome" in roles["Shadow receiver"]["about"]
    assert "explain its scheme" in roles["Sender exit interview"]["about"]
    assert "Flagged: blocked" in page
    assert "Judge: names the target" in page
    assert "plain episodes hide nothing" in page
    assert page.index('add(card,"h2","What happened")') < page.index('add(seen,"h2","As seen by")')
    assert "As sent" not in page
    assert page.index('add(seen,"h2","As seen by")') < page.index('add(log,"h2","Attempt log as sender saw it")')
