"""The API client generator: spec text in, client out.

Everything openapi-python-client touches is under this package, so the
runtime never imports it. A spec is either rendered into memory for one
run or written into an executable archive that carries the CLI with it.
"""

import pathlib

from imbi_cli.generator import archive as _archive
from imbi_cli.generator import openapi as _openapi
from imbi_cli.generator import render as _render

__all__ = ["generate", "render"]


def render(spec: str) -> None:
    """Render the document into memory and bind it as the client."""
    _render.build(_openapi.normalize(spec))


def generate(spec: str, archive: pathlib.Path) -> None:
    """Render the document into an executable archive."""
    _archive.generate(_openapi.normalize(spec), archive)
