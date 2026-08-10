"""The rendered version strings, and the version the generator bakes in."""

import json
import pathlib

import pytest

from imbi_cli import app
from imbi_cli.generator import archive


def _settings(**overrides: object) -> app.Settings:
    defaults: dict[str, object] = {
        "url": None,
        "token": None,
        "spec_path": "openapi.json",
        "spec_file": None,
        "timeout": 30.0,
        "organization": None,
        "verbose": False,
    }
    return app.Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_version_line_names_the_cli_alone_when_nothing_is_baked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", None)
    assert app.version_line() == f"imbi-cli {app.__version__}"
    assert app.description() == "Imbi API CLI"


def test_version_line_names_the_baked_imbi_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", "2.22.0")
    assert app.version_line() == f"imbi-cli {app.__version__} (Imbi 2.22.0)"


def test_description_names_the_baked_imbi_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", "2.22.0")
    assert app.description() == "Imbi API CLI, generated from Imbi 2.22.0"


def test_staged_source_bakes_the_documents_info_version() -> None:
    source = archive._staged_source({"info": {"version": "2.22.0"}})
    assert "SERVER_VERSION: str | None = '2.22.0'" in source
    assert archive._VERSION_LINE not in source


def test_staged_source_bakes_archived_without_an_info_version() -> None:
    assert "\nARCHIVED = True\n" in archive._staged_source({})
    assert archive._ARCHIVED_LINE not in archive._staged_source({})


def test_spec_version_reads_info_version() -> None:
    text = json.dumps({"info": {"title": "Imbi", "version": "2.22.0"}})
    assert app.spec_version(text) == "2.22.0"


def test_spec_version_is_none_for_a_spec_without_one() -> None:
    assert app.spec_version(json.dumps({"info": {"title": "Imbi"}})) is None
    assert app.spec_version(json.dumps({"openapi": "3.1.0"})) is None


def test_spec_version_is_none_for_text_that_is_not_json() -> None:
    assert app.spec_version("<html>not a spec</html>") is None


def test_bind_server_version_keeps_a_baked_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", "2.22.0")
    app.bind_server_version("1.0.0")
    assert app.SERVER_VERSION == "2.22.0"


def test_bind_server_version_ignores_an_absent_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", None)
    app.bind_server_version(None)
    app.bind_server_version("")
    assert app.SERVER_VERSION is None


def test_resolve_server_version_reads_the_spec_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", None)
    monkeypatch.setattr(app, "installed_version", lambda: None)
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(
        json.dumps({"info": {"version": "2.21.0"}}), encoding="utf-8"
    )
    app.resolve_server_version(_settings(spec_file=spec_file))
    assert app.SERVER_VERSION == "2.21.0"


def test_resolve_server_version_survives_having_no_spec_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", None)
    monkeypatch.setattr(app, "installed_version", lambda: None)
    app.resolve_server_version(_settings())
    assert app.SERVER_VERSION is None


def test_resolve_server_version_survives_an_unreachable_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "SERVER_VERSION", None)
    monkeypatch.setattr(app, "installed_version", lambda: None)

    def unreachable(settings: app.Settings) -> str:
        raise app.SpecUnavailable("could not fetch the OpenAPI spec")

    monkeypatch.setattr(app, "spec", unreachable)
    app.resolve_server_version(_settings(url="https://imbi.example.com"))
    assert app.SERVER_VERSION is None
