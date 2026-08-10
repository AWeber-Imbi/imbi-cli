"""The default archive name build-pyz writes to."""

import pathlib

from imbi_cli import app


def test_default_archive_name_prefixes_the_server_version() -> None:
    assert app.default_archive_name("2.22.0") == pathlib.Path(
        "imbi-cli_v2.22.0.pyz"
    )


def test_default_archive_name_keeps_an_existing_v_prefix() -> None:
    assert app.default_archive_name("v2.22.0") == pathlib.Path(
        "imbi-cli_v2.22.0.pyz"
    )


def test_default_archive_name_accepts_pre_release_and_build() -> None:
    assert app.default_archive_name("2.22.0-rc.1+abc123") == pathlib.Path(
        "imbi-cli_v2.22.0-rc.1+abc123.pyz"
    )


def test_default_archive_name_is_unknown_without_a_server_version() -> None:
    assert app.default_archive_name(None) == pathlib.Path(
        "imbi-cli_unknown.pyz"
    )


def test_default_archive_name_is_unknown_for_a_version_not_semver() -> None:
    for version in ("2.22", "2", "abc", "2.22.0.1", ""):
        assert app.default_archive_name(version) == pathlib.Path(
            "imbi-cli_unknown.pyz"
        )
