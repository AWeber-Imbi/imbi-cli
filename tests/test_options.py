"""The global options, and the ones an archive does not offer."""

import argparse

import pytest

from imbi_cli import app

_ARCHIVE_ONLY = ("--spec-path", "--spec-file")
_ALWAYS = (
    "--url",
    "--token",
    "--organization",
    "--timeout",
    "--env-file",
    "-v",
    "--verbose",
    "--version",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=app.PROG_NAME)
    app.add_global_options(parser)
    return parser


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in action.option_strings
    }


def test_help_offers_the_spec_and_archive_options_when_installed() -> None:
    assert set((*_ALWAYS, *_ARCHIVE_ONLY)) <= _option_strings(_parser())


def test_help_hides_the_spec_and_archive_options_when_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "ARCHIVED", True)
    options = _option_strings(_parser())
    assert set(_ALWAYS) <= options
    assert options.isdisjoint(_ARCHIVE_ONLY)


def test_the_unavailable_parser_hides_them_too_when_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "ARCHIVED", True)
    options = _option_strings(app.unavailable_parser("no client"))
    assert set(_ALWAYS) <= options
    assert options.isdisjoint(_ARCHIVE_ONLY)


def test_an_archive_rejects_the_options_it_does_not_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(app, "ARCHIVED", True)
    with pytest.raises(SystemExit) as caught:
        _parser().parse_args(["--spec-file", "spec.json"])
    assert caught.value.code == app.USAGE_EXIT
    captured = capsys.readouterr()
    assert "unrecognized arguments: --spec-file" in captured.err


def test_the_pre_parse_tolerates_an_option_an_archive_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "ARCHIVED", True)
    globals_only = argparse.ArgumentParser(prog=app.PROG_NAME, add_help=False)
    app.add_global_options(globals_only)
    namespace, unknown = globals_only.parse_known_args(
        ["--spec-file", "spec.json", "projects", "list-projects"]
    )
    assert namespace.version is False
    assert "--spec-file" in unknown


def test_resolve_defaults_the_options_an_archive_leaves_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "ARCHIVED", True)
    namespace, _ = _parser().parse_known_args([])
    settings = app.resolve(namespace)
    assert settings.spec_path == "openapi.json"
    assert settings.spec_file is None
