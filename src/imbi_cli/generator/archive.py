"""The executable archive: the generated client plus the CLI itself.

The CLI is resolved by import path, read from its loader, and staged
beside the generated client under the name the archive imports it by, so
nothing here opens a path of its own.
"""

import contextlib
import importlib.abc
import importlib.metadata
import importlib.util
import io
import pathlib
import tempfile
import tomllib
import typing
import zipapp

from openapi_python_client import config as opc_config

from imbi_cli import app
from imbi_cli.generator import render

# the import path of the CLI's single module
RUNTIME = "imbi_cli.app"

_EXCLUDED = frozenset({"README.md", ".gitignore"})

# a shebang is truncated past 127 bytes on Linux
_SHEBANG_LIMIT = 127

# the source lines the staged module is baked from
_VERSION_LINE = "SERVER_VERSION: str | None = None"
_ARCHIVED_LINE = "ARCHIVED = False"


def _runtime_source() -> str:
    """The CLI module's source, read from the loader that imports it."""
    module_spec = importlib.util.find_spec(RUNTIME)
    if module_spec is None or module_spec.loader is None:
        raise app.SpecError(f"cannot locate {RUNTIME} to stage")
    source = typing.cast(
        importlib.abc.InspectLoader, module_spec.loader
    ).get_source(RUNTIME)
    if source is None:
        raise app.SpecError(f"cannot read the source of {RUNTIME}")
    return source


def _bake(source: str, line: str, replacement: str) -> str:
    """One named source line replaced by what the archive needs.

    Raises ``SpecError`` when the source holds no such line.
    """
    if line not in source:
        raise app.SpecError(f"{RUNTIME} has no {line!r} line to bake into")
    return source.replace(line, replacement, 1)


def _staged_source(document: dict) -> str:
    """The CLI's source as an archive stages it.

    ``ARCHIVED`` is always baked true; the document's ``info.version``
    is baked in when it names one.

    Raises ``SpecError`` when the source has no line to bake into.
    """
    source = _bake(_runtime_source(), _ARCHIVED_LINE, "ARCHIVED = True")
    version = document.get("info", {}).get("version")
    if not version:
        return source
    return _bake(
        source,
        _VERSION_LINE,
        f"SERVER_VERSION: str | None = {version!r}",
    )


def _interpreter(project_file: pathlib.Path) -> str:
    """The shebang that runs the archive under ``uv``.

    ``uv`` installs the generated client's dependencies on first run, so
    the archive needs nothing on the interpreter it lands on. The
    dependencies come from the rendered project and the interpreter
    constraint from this CLI's own metadata, which the archived module
    shares.

    Raises ``SpecError`` when the shebang would be truncated.
    """
    metadata = tomllib.loads(project_file.read_text(encoding="utf-8"))
    requires = metadata["project"]["dependencies"]
    # env -S splits the shebang on whitespace, so a multi-clause
    # specifier has to reach uv as one word.
    python = importlib.metadata.metadata(app.PROG_NAME)[
        "Requires-Python"
    ].replace(" ", "")
    arguments = " ".join(f"--with {each}" for each in requires)
    shebang = (
        f"/usr/bin/env -S uv run --no-project "
        f"--python {python} {arguments} python"
    )
    if len(shebang.encode()) > _SHEBANG_LIMIT:
        raise app.SpecError(
            f"the archive's shebang is {len(shebang.encode())} bytes, "
            f"over the {_SHEBANG_LIMIT} a kernel reads: {shebang}"
        )
    return shebang


def generate(document: dict, archive: pathlib.Path) -> None:
    """Render the document into ``archive``, one executable zipapp.

    The document is rendered into a temporary uv project, the CLI is
    staged beside the generated client, and the result is zipped with
    the entry point ``zipapp`` writes, behind a shebang that runs it
    under ``uv``. Missing parent directories are created and an existing
    archive is replaced only once the new one is complete, so a failed
    build never truncates it.

    Raises ``SpecError`` when the document describes no usable API.
    """
    with tempfile.TemporaryDirectory() as directory:
        output = pathlib.Path(directory) / app.PACKAGE_NAME.replace("_", "-")
        rendered = render.project(document, opc_config.MetaType.UV, output)
        # Project.build announces the output directory on stdout, which
        # belongs to whatever command the CLI is running.
        with contextlib.redirect_stdout(io.StringIO()):
            errors = rendered.build()
        if errors:
            render.fail(errors)
        (output / f"{app.ENTRY}.py").write_text(
            _staged_source(document), encoding="utf-8"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        partial = archive.with_name(f"{archive.name}.partial")
        try:
            zipapp.create_archive(
                output,
                target=partial,
                interpreter=_interpreter(output / "pyproject.toml"),
                main=f"{app.ENTRY}:main",
                filter=lambda path: path.parts[0] not in _EXCLUDED,
            )
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(archive)
