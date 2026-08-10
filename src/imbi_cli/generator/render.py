"""The API client rendered from the OpenAPI document, in memory.

The CLI has no checked-in API client: openapi-python-client renders the
normalized document into Python source, and the command tree is built
from whatever it describes. Nothing is written to disk -- the rendered
modules live in memory, served by a ``sys.meta_path`` finder, and their
packages are walkable through a matching ``sys.path_hooks`` entry.

The rendered source is executed, so the document is only as trusted as
where it came from: the Imbi instance the user named and already sends
their API token to, or a spec file they saved themselves.
"""

import collections.abc
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import pathlib
import sys
import types
import typing

import openapi_python_client
from openapi_python_client import config as opc_config
from openapi_python_client import parser as opc_parser
from openapi_python_client import utils as opc_utils
from openapi_python_client.parser import errors as opc_errors
from openapi_python_client.parser import properties as opc_properties

from imbi_cli import app

_PATH_PREFIX = "<imbi-cli generated>/"


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.InspectLoader):
    """Imports generated modules from a name -> source mapping."""

    def __init__(self, sources: dict[str, str], packages: set[str]) -> None:
        self.sources = sources
        self.packages = packages

    def find_spec(
        self,
        fullname: str,
        path: typing.Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname not in self.sources:
            return None
        is_package = fullname in self.packages
        spec = importlib.util.spec_from_loader(
            fullname, self, is_package=is_package
        )
        if spec is not None and is_package:
            # pkgutil walks a package through its search locations, so
            # each one names the package a path hook resolves back to.
            spec.submodule_search_locations = [f"{_PATH_PREFIX}{fullname}"]
        return spec

    def get_source(self, fullname: str) -> str:
        try:
            return self.sources[fullname]
        except KeyError:
            raise ImportError(fullname, name=fullname) from None

    def get_code(self, fullname: str) -> types.CodeType:
        return compile(self.get_source(fullname), f"<{fullname}>", "exec")


class _PathEntry:
    """One rendered package, as ``pkgutil`` wants to see it."""

    def __init__(self, finder: _Finder, fullname: str) -> None:
        self.finder = finder
        self.fullname = fullname

    def find_spec(
        self, fullname: str, target: types.ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        return self.finder.find_spec(fullname, None, target)

    def iter_modules(
        self, prefix: str = ""
    ) -> collections.abc.Iterator[tuple[str, bool]]:
        base = f"{self.fullname}."
        for name in sorted(self.finder.sources):
            child = name.removeprefix(base)
            if not name.startswith(base) or "." in child:
                continue
            yield f"{prefix}{child}", name in self.finder.packages


def _path_hook(
    finder: _Finder,
) -> collections.abc.Callable[[str], _PathEntry]:
    def hook(path: str) -> _PathEntry:
        if not isinstance(path, str) or not path.startswith(_PATH_PREFIX):
            raise ImportError(path)
        return _PathEntry(finder, path.removeprefix(_PATH_PREFIX))

    return hook


def fail(
    errors: collections.abc.Iterable[opc_errors.GeneratorError],
) -> typing.NoReturn:
    """Raise ``SpecError``, naming every generator error."""
    details = "\n  ".join(str(error) for error in errors)
    raise app.SpecError(f"could not build an API client:\n  {details}")


def _sources(project: typing.Any) -> tuple[dict[str, str], set[str]]:
    """Render a built Project's templates to module sources.

    Mirrors ``Project._create_package``/``_build_models``/``_build_api``,
    swapping each ``write_text`` for a dict entry.
    """
    name = app.PACKAGE_NAME
    env, openapi = project.env, project.openapi
    prefix = project.config.field_prefix
    sources = {
        name: env.get_template("package_init.py.jinja").render(),
        f"{name}.types": env.get_template("types.py.jinja").render(),
        f"{name}.client": env.get_template("client.py.jinja").render(),
        f"{name}.errors": env.get_template("errors.py.jinja").render(),
    }
    packages = {name, f"{name}.models", f"{name}.api"}

    model_template = env.get_template("model.py.jinja")
    enum_templates = {
        "literal": env.get_template("literal_enum.py.jinja"),
        "int": env.get_template("int_enum.py.jinja"),
        "str": env.get_template("str_enum.py.jinja"),
    }
    imports, alls = [], []
    for model in openapi.models:
        module = f"{name}.models.{model.class_info.module_name}"
        sources[module] = model_template.render(model=model)
        imports.append(opc_parser.import_string_from_class(model.class_info))
        alls.append(model.class_info.name)
    for generated_enum in openapi.enums:
        if isinstance(generated_enum, opc_properties.LiteralEnumProperty):
            template = enum_templates["literal"]
        elif generated_enum.value_type is int:
            template = enum_templates["int"]
        else:
            template = enum_templates["str"]
        module = f"{name}.models.{generated_enum.class_info.module_name}"
        sources[module] = template.render(enum=generated_enum)
        imports.append(
            opc_parser.import_string_from_class(generated_enum.class_info)
        )
        alls.append(generated_enum.class_info.name)
    sources[f"{name}.models"] = env.get_template(
        "models_init.py.jinja"
    ).render(imports=imports, alls=alls)

    sources[f"{name}.api"] = env.get_template("api_init.py.jinja").render()
    endpoint_init_template = env.get_template("endpoint_init.py.jinja")
    endpoint_template = env.get_template(
        "endpoint_module.py.jinja",
        globals={"isbool": lambda obj: obj.get_base_type_string() == "bool"},
    )
    for tag, collection in openapi.endpoint_collections_by_tag.items():
        tag_name = f"{name}.api.{tag}"
        packages.add(tag_name)
        sources[tag_name] = endpoint_init_template.render(
            endpoint_collection=collection
        )
        for endpoint in collection.endpoints:
            module = opc_utils.PythonIdentifier(endpoint.name, prefix)
            sources[f"{tag_name}.{module}"] = endpoint_template.render(
                endpoint=endpoint
            )
    return sources, packages


def project(
    document: dict,
    meta: opc_config.MetaType,
    output: pathlib.Path,
) -> typing.Any:
    """The built project for one config.

    Raises ``SpecError``, naming every parse error.
    """
    config = opc_config.Config.from_sources(
        config_file=opc_config.ConfigFile(
            package_name_override=app.PACKAGE_NAME,
            project_name_override=app.DIST_NAME,
            post_hooks=[],
        ),
        meta_type=meta,
        document_source="openapi",
        file_encoding="utf-8",
        overwrite=True,
        output_path=output,
    )

    openapi = opc_parser.GeneratorData.from_dict(document, config=config)
    if isinstance(openapi, opc_errors.GeneratorError):
        fail([openapi])
    built = openapi_python_client.Project(openapi=openapi, config=config)
    errors = [
        *openapi.errors,
        *(
            error
            for collection in openapi.endpoint_collections_by_tag.values()
            for error in collection.parse_errors
        ),
    ]
    if errors:
        fail(errors)
    return built


def build(document: dict) -> None:
    """Render the document into memory and bind the package once."""
    if app.client_available():
        return
    rendered = project(
        document, opc_config.MetaType.NONE, pathlib.Path(app.PACKAGE_NAME)
    )
    finder = _Finder(*_sources(rendered))
    sys.meta_path.insert(0, finder)
    sys.path_hooks.insert(0, _path_hook(finder))
    app.bind_client(importlib.import_module(app.PACKAGE_NAME))
