"""The whole CLI: argv in, exit code out.

Settings, HTTP policy, output, the generated client accessors, the
parser built from the OpenAPI document, and every Imbi-specific
departure from it live here, in one module. Nothing in this file imports
the rest of the package -- the only deferred import is
``imbi_cli.generator``, and it fails with the reason a client cannot be
rendered -- so the generator can copy this one file into an archive and
run it there.
"""

import argparse
import collections.abc
import contextlib
import datetime
import enum
import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import pathlib
import pkgutil
import re
import sys
import types
import typing
import urllib.parse

import httpx

__version__ = "0.2.0"

# The Imbi version this module's client was generated from. The
# generator bakes it into the copy it stages inside an archive; it stays
# None when imbi-cli runs from its own installation.
SERVER_VERSION: str | None = None

# True only in the copy the generator stages inside an archive, which
# carries its own client and so takes no spec or archive options.
ARCHIVED = False

PROG_NAME = "imbi-cli"
PACKAGE_NAME = "imbi_api_client"

# the name the CLI module is staged and imported under in an archive
ENTRY = "imbi"

# the distribution a rendered client is installed as; its version is the
# spec's info.version
DIST_NAME = PACKAGE_NAME.replace("_", "-")

USAGE_EXIT = 2
LOGGER_NAME = "imbi_cli"
ENV_FILE_VAR = "IMBI_ENV_FILE"

MISSING = object()

Handler = collections.abc.Callable[[argparse.Namespace], None]

_DESCRIPTION = "Imbi API CLI"
_REDACTED_HEADERS = frozenset({"authorization", "private-token"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_NONE_TYPE = type(None)

_root: types.ModuleType | None = None

logger = logging.getLogger(f"{LOGGER_NAME}.http")


def version_line() -> str:
    """The version, naming the Imbi version only when one is baked in."""
    if SERVER_VERSION is None:
        return f"{PROG_NAME} {__version__}"
    return f"{PROG_NAME} {__version__} (Imbi {SERVER_VERSION})"


def description() -> str:
    """The description, naming a baked-in Imbi version when there is one."""
    if SERVER_VERSION is None:
        return _DESCRIPTION
    return f"{_DESCRIPTION}, generated from Imbi {SERVER_VERSION}"


class SpecError(Exception):
    """The document describes no usable API."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SpecUnavailable(Exception):
    """There is no OpenAPI document, and why there is none."""


class Target(typing.NamedTuple):
    """The Imbi instance a run talks to, and how."""

    url: str | None
    token: str | None
    timeout: float
    spec_path: str


class Settings(typing.NamedTuple):
    """Every default the CLI has, resolved once."""

    url: str | None
    token: str | None
    spec_path: str
    spec_file: pathlib.Path | None
    timeout: float
    organization: str | None
    verbose: bool

    def target(self) -> Target:
        """The Imbi instance these settings name, and how to reach it."""
        return Target(
            url=self.url,
            token=self.token,
            timeout=self.timeout,
            spec_path=self.spec_path,
        )


def fail(message: str, code: int = USAGE_EXIT) -> typing.NoReturn:
    """Report a problem on stderr and exit.

    A usage problem is prefixed and exits 2, the way argparse reports the
    ones it catches itself; anything else failed at runtime and exits 1.
    """
    prefix = f"{PROG_NAME}: error: " if code == USAGE_EXIT else ""
    print(f"{prefix}{message}", file=sys.stderr)
    raise SystemExit(code)


def emit(response: typing.Any) -> None:
    """Print a generated ``Response``, exiting non-zero on HTTP >= 400."""
    parsed = MISSING
    if response.content:
        try:
            parsed = json.loads(response.content)
        except ValueError:
            pass
    if response.status_code >= 400:
        body = (
            json.dumps(parsed, indent=2, ensure_ascii=False)
            if parsed is not MISSING
            else response.content.decode("utf-8", "replace")
        )
        fail(f"HTTP {response.status_code}: {body}", code=1)
    if parsed is not MISSING:
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    elif response.content:
        print(response.content.decode("utf-8", "replace"))


def parsed_list(response: typing.Any) -> list | None:
    """The parsed list body, or None once an error has been emitted."""
    if response.status_code < 400 and isinstance(response.parsed, list):
        return response.parsed
    emit(response)
    return None


def init_logging(verbose: bool) -> None:
    """Attach a stderr handler to the imbi_cli logger (idempotent)."""
    root = logging.getLogger(LOGGER_NAME)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def kebab(name: str) -> str:
    return name.rstrip("_").replace("_", "-")


def auth_headers(token: str | None) -> dict[str, str]:
    """Bearer headers for the token, empty when there is none."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def redact_headers(headers: httpx.Headers) -> dict[str, str]:
    """Header values with credentials replaced by ``***``."""
    return {
        name: ("***" if name.lower() in _REDACTED_HEADERS else value)
        for name, value in headers.items()
    }


def log_request(request: httpx.Request) -> None:
    """Log the request line and redacted headers, never the body.

    Request bodies can carry configuration secrets, so they stay out of
    the log even at DEBUG.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(
        "request method=%s url=%s headers=%s",
        request.method,
        request.url,
        redact_headers(request.headers),
    )


def log_response(response: httpx.Response) -> None:
    """Log the status and redacted headers, never the body.

    Response bodies can carry plaintext configuration values, so they
    stay out of the log even at DEBUG.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    logger.debug(
        "response status=%s headers=%s",
        response.status_code,
        redact_headers(response.headers),
    )


def event_hooks() -> dict[str, list]:
    """httpx event hooks that log every request and response."""
    return {"request": [log_request], "response": [log_response]}


def fetch_spec(target: Target) -> str:
    """GET the target's spec path, relative to its URL.

    Raises ``httpx.HTTPError`` -- an unreachable instance and an error
    status alike -- and never exits.
    """
    with httpx.Client(
        base_url=str(target.url),
        headers=auth_headers(target.token),
        timeout=target.timeout,
        event_hooks=event_hooks(),
        follow_redirects=True,
    ) as client:
        response = client.get(target.spec_path)
        response.raise_for_status()
        return response.text


def client_available() -> bool:
    """True once the generated client package can be imported."""
    if _root is not None:
        return True
    try:
        return importlib.util.find_spec(PACKAGE_NAME) is not None
    except ImportError, ValueError:
        return False


def bind_client(root: types.ModuleType) -> None:
    """Bind an already-imported package as the client."""
    global _root
    _root = root


def _import(name: str) -> types.ModuleType:
    """Import a module from the generated package."""
    if _root is None:
        raise SpecError("the API client has not been generated")
    return importlib.import_module(name)


def _submodules(fullname: str) -> list[str]:
    module = importlib.import_module(fullname)
    return sorted(
        found.name for found in pkgutil.iter_modules(module.__path__)
    )


def tags() -> list[str]:
    """The OpenAPI tag names the document describes."""
    _import(f"{PACKAGE_NAME}.api")
    return _submodules(f"{PACKAGE_NAME}.api")


def operations(tag: str) -> list[str]:
    """The operation names one tag describes."""
    _import(f"{PACKAGE_NAME}.api.{tag}")
    return _submodules(f"{PACKAGE_NAME}.api.{tag}")


def operation(tag: str, name: str) -> types.ModuleType:
    """The generated module for one tag's operation."""
    return _import(f"{PACKAGE_NAME}.api.{tag}.{name}")


def model(name: str) -> typing.Any:
    """A model class from the generated ``models`` package."""
    return getattr(_import(f"{PACKAGE_NAME}.models"), name)


def unset_type() -> type:
    """The generated ``Unset`` type."""
    return _import(f"{PACKAGE_NAME}.types").Unset


def authenticated_client(target: Target) -> typing.Any:
    """The generated ``AuthenticatedClient`` for a target.

    It has no static type: the API client is generated at runtime from
    the document.
    """
    errors = []
    if not target.token:
        errors.append(
            "IMBI_TOKEN is not set. Create an API key in Imbi and "
            "export it as IMBI_TOKEN."
        )
    if target.url is None:
        errors.append(
            "--url is not set (e.g. --url https://imbi.example.com, "
            "or IMBI_URL)."
        )
    if errors:
        fail("\n".join(errors), code=1)

    return _import(f"{PACKAGE_NAME}.client").AuthenticatedClient(
        base_url=str(target.url),
        token=target.token,
        timeout=httpx.Timeout(target.timeout),
        httpx_args={"event_hooks": event_hooks()},
    )


@contextlib.contextmanager
def connect(target: Target) -> collections.abc.Iterator[typing.Any]:
    """An API client, turning transport failures into exit code 1."""
    try:
        with authenticated_client(target) as api_client:
            yield api_client
    except httpx.HTTPError as exc:
        fail(f"Request failed: {exc!r}", code=1)


def load_env(named: pathlib.Path | str | None = None) -> None:
    """Load the dotenv file named, or the one ``IMBI_ENV_FILE`` names.

    ``KEY=VALUE`` lines land in ``os.environ`` without overwriting
    anything already set. Blank lines, ``#`` comments, and a leading
    ``export`` are ignored, and quotes around a value are stripped.
    """
    named = named or os.environ.get(ENV_FILE_VAR)
    if not named:
        return
    try:
        text = pathlib.Path(named).read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"could not read the env file {named}: {exc}")
    for raw in text.splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        os.environ.setdefault(name.strip(), value)


def url_arg(value: str) -> str:
    """An http or https URL, rejecting anything else."""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an http or https URL"
        )
    return value


def timeout_arg(value: str) -> float:
    """A positive number of seconds."""
    try:
        timeout = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a number"
        ) from None
    if timeout <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return timeout


def add_global_options(parser: argparse.ArgumentParser) -> None:
    """Add every option the settings are resolved from."""
    parser.add_argument(
        "--url", type=url_arg, help="Imbi server URL [env: IMBI_URL]"
    )
    parser.add_argument("--token", help="Imbi API token [env: IMBI_TOKEN]")
    parser.add_argument(
        "--organization",
        help="Default organization slug [env: IMBI_ORGANIZATION]",
    )
    if not ARCHIVED:
        parser.add_argument(
            "--spec-path",
            help="Path to the OpenAPI document, relative to the URL "
            "(default: openapi.json) [env: IMBI_SPEC_PATH]",
        )
        parser.add_argument(
            "--spec-file",
            type=pathlib.Path,
            help="Read the OpenAPI spec from this file [env: IMBI_SPEC_FILE]",
        )
    parser.add_argument(
        "--timeout",
        type=timeout_arg,
        help="HTTP timeout in seconds (default: 30) [env: IMBI_TIMEOUT]",
    )
    parser.add_argument(
        "--env-file",
        type=pathlib.Path,
        help=f"Path to a dotenv file to load [env: {ENV_FILE_VAR}]",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log HTTP requests and responses (never their bodies) to "
        "stderr [env: IMBI_VERBOSE]",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the version and exit",
    )


def _environ(name: str) -> str | None:
    return os.environ.get(f"IMBI_{name}") or None


def _env_value(
    name: str, parse: collections.abc.Callable[[str], typing.Any]
) -> typing.Any:
    """One environment variable, parsed the way its option is."""
    raw = _environ(name)
    if raw is None:
        return None
    try:
        return parse(raw)
    except (argparse.ArgumentTypeError, ValueError) as exc:
        fail(f"IMBI_{name}: {exc}")


def resolve(namespace: argparse.Namespace) -> Settings:
    """The settings a run uses: command line, environment, dotenv file.

    ``load_env`` has already merged the dotenv file into the environment
    without overwriting it, so the environment covers both.
    """
    verbose = namespace.verbose or (
        (_environ("VERBOSE") or "").strip().lower() in _TRUE_VALUES
    )
    return Settings(
        url=namespace.url or _env_value("URL", url_arg),
        token=namespace.token or _environ("TOKEN"),
        spec_path=getattr(namespace, "spec_path", None)
        or _environ("SPEC_PATH")
        or "openapi.json",
        spec_file=getattr(namespace, "spec_file", None)
        or _env_value("SPEC_FILE", pathlib.Path),
        timeout=namespace.timeout
        or _env_value("TIMEOUT", timeout_arg)
        or 30.0,
        organization=namespace.organization or _environ("ORGANIZATION"),
        verbose=verbose,
    )


def api_parameters(
    function: collections.abc.Callable,
) -> dict[str, inspect.Parameter]:
    """A generated API function's parameters, minus ``client``."""
    return {
        name: parameter
        for name, parameter in inspect.signature(function).parameters.items()
        if name != "client"
    }


def value_members(annotation: typing.Any) -> list[typing.Any]:
    """Union members that carry a value (drop None and Unset)."""
    unset = unset_type()
    members = (
        list(typing.get_args(annotation))
        if typing.get_origin(annotation) in (types.UnionType, typing.Union)
        else [annotation]
    )
    return [
        member
        for member in members
        if member is not _NONE_TYPE and member is not unset
    ]


def _base_type(annotation: typing.Any) -> typing.Any:
    """The type an annotation's value is built from."""
    members = value_members(annotation)
    return members[0] if members else str


def argument_kwargs(annotation: typing.Any) -> dict[str, typing.Any]:
    """The argparse keywords for one API parameter's annotation.

    An enum becomes choices of the values the API accepts, a bool a
    ``--flag``/``--no-flag`` pair, a list an option that repeats, and a
    datetime an ISO 8601 string.
    """
    base = _base_type(annotation)
    if typing.get_origin(base) is list:
        (item,) = typing.get_args(base)
        return {**argument_kwargs(item), "action": "append"}
    if isinstance(base, type) and issubclass(base, enum.Enum):
        values = [member.value for member in base]
        return {
            "choices": values,
            "type": type(values[0]) if values else str,
        }
    if base is bool:
        return {"action": argparse.BooleanOptionalAction}
    if isinstance(base, type) and issubclass(base, datetime.datetime):
        return {"type": datetime.datetime.fromisoformat}
    if isinstance(base, type) and issubclass(base, datetime.date):
        return {"type": datetime.date.fromisoformat}
    if base in (int, float, str):
        return {"type": base}
    return {"type": str}


def add_argument(
    parser: argparse.ArgumentParser,
    name: str,
    parameter: inspect.Parameter,
) -> None:
    """Add the option for one non-body API function parameter.

    A required parameter becomes a required option; an optional one
    defaults to ``argparse.SUPPRESS``, so an omitted flag stays out of
    the namespace and the API function falls back to ``UNSET``.
    """
    alias = name.rstrip("_")
    parser.add_argument(
        f"--{kebab(alias)}",
        dest=name,
        help=alias,
        default=argparse.SUPPRESS,
        required=parameter.default is inspect.Parameter.empty,
        **argument_kwargs(parameter.annotation),
    )


def api_value(value: typing.Any, annotation: typing.Any) -> typing.Any:
    """Convert a parsed CLI value to what the API function expects."""
    base = _base_type(annotation)
    if typing.get_origin(base) is list:
        (item,) = typing.get_args(base)
        return [api_value(member, item) for member in value]
    if isinstance(base, type) and issubclass(base, enum.Enum):
        return base(value)
    return value


def api_kwargs(
    namespace: argparse.Namespace,
    parameters: dict[str, inspect.Parameter],
) -> dict[str, typing.Any]:
    """Collect the API params to pass on from a parsed command line.

    An option the user left off is absent from the namespace, so the
    generated function falls back to its own default (``UNSET``).
    """
    kwargs: dict[str, typing.Any] = {}
    for name, parameter in parameters.items():
        if not hasattr(namespace, name):
            continue
        kwargs[name] = api_value(
            getattr(namespace, name), parameter.annotation
        )
    return kwargs


def convert_body(raw: typing.Any, annotation: typing.Any) -> typing.Any:
    """Build the generated model(s) for a request body from parsed JSON."""
    failures = []
    for member in value_members(annotation):
        try:
            if typing.get_origin(member) is list:
                (item,) = typing.get_args(member)
                return [item.from_dict(value) for value in raw]
            if hasattr(member, "from_dict"):
                return member.from_dict(raw)
        except Exception as exc:  # noqa: BLE001 - report all candidates
            failures.append(f"{member.__name__}: {exc}")
    if failures:
        fail(
            "body does not match the expected schema:\n  "
            + "\n  ".join(failures)
        )
    return raw


def command_help(function: collections.abc.Callable) -> str:
    doc = inspect.getdoc(function) or ""
    return doc.split("\n\nArgs:")[0].split("\n\nRaises:")[0].strip()


def short_help(help_text: str, fallback: str) -> str:
    return (help_text or fallback).splitlines()[0]


def add_body_options(parser: argparse.ArgumentParser) -> None:
    """Add the mutually exclusive JSON body sources."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--body", help="Inline JSON body")
    group.add_argument(
        "--body-file", type=pathlib.Path, help="Path to a JSON body file"
    )
    group.add_argument(
        "--body-stdin",
        action="store_true",
        help="Read the JSON body from stdin",
    )


def json_body(namespace: argparse.Namespace) -> typing.Any:
    """The parsed body, or ``MISSING`` when no source was named."""
    try:
        if namespace.body is not None:
            return json.loads(namespace.body)
        if namespace.body_file is not None:
            return json.loads(namespace.body_file.read_text(encoding="utf-8"))
        if namespace.body_stdin:
            return json.loads(sys.stdin.read())
    except ValueError as exc:
        fail(f"invalid JSON body: {exc}")
    except OSError as exc:
        fail(f"could not read the JSON body: {exc}")
    return MISSING


def api_handler(
    function: collections.abc.Callable,
    api_parameters: dict[str, inspect.Parameter],
    body: inspect.Parameter | None,
    target: Target,
) -> Handler:
    """The handler that calls one generated API function."""

    def handler(namespace: argparse.Namespace) -> None:
        kwargs = api_kwargs(namespace, api_parameters)
        if body is not None:
            raw = json_body(namespace)
            if raw is MISSING:
                if body.default is inspect.Parameter.empty:
                    fail(
                        "a body is required: use --body, --body-file, "
                        "or --body-stdin"
                    )
            else:
                kwargs["body"] = convert_body(raw, body.annotation)
        with connect(target) as api_client:
            emit(function(client=api_client, **kwargs))

    return handler


def command_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    module: types.ModuleType,
    target: Target,
    organization: str | None,
) -> argparse.ArgumentParser:
    """The parser for one generated operation."""
    function = module.sync_detailed
    parameters = api_parameters(function)
    body = parameters.pop("body", None)
    help_text = command_help(function)
    parser = subparsers.add_parser(
        name,
        help=short_help(help_text, name),
        description=help_text or None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for param_name, parameter in parameters.items():
        override = FIELD_OVERRIDES.get(param_name)
        if override is not None:
            override(parser, organization)
        else:
            add_argument(parser, param_name, parameter)
    if body is not None:
        add_body_options(parser)
    parser.set_defaults(
        handler=api_handler(function, parameters, body, target)
    )
    return parser


def tag_parser(
    subparsers: argparse._SubParsersAction,
    tag: str,
    target: Target,
    organization: str | None,
) -> argparse.ArgumentParser:
    """One tag's operations, minus HIDDEN, plus SYNTHETIC."""
    parser = subparsers.add_parser(
        kebab(tag), help=tag.replace("_", " ").title()
    )
    commands = parser.add_subparsers(
        dest="command", metavar="COMMAND", required=True
    )
    named = {}
    for name in operations(tag):
        if (tag, name) in HIDDEN:
            continue
        named[kebab(RENAMES.get((tag, name), name))] = name
    synthetic = SYNTHETIC.get(tag, {})
    for name in sorted([*named, *synthetic]):
        factory = synthetic.get(name)
        if factory is not None:
            factory(commands, name, target, organization)
            continue
        command = command_parser(
            commands, name, operation(tag, named[name]), target, organization
        )
        augment = AUGMENTS.get((tag, named[name]))
        if augment is not None:
            command.set_defaults(handler=augment(target))
    return parser


def add_org_slug(
    parser: argparse.ArgumentParser, organization: str | None
) -> None:
    """The org slug option, defaulted from the environment when set."""
    help_text = "org_slug [env: IMBI_ORGANIZATION]"
    if organization is None:
        parser.add_argument(
            "--org-slug", dest="org_slug", required=True, help=help_text
        )
        return
    parser.add_argument(
        "--org-slug",
        dest="org_slug",
        default=organization,
        help=help_text,
    )


def _fetch_values(
    api_client: typing.Any, keys: list[str], **kwargs: typing.Any
) -> typing.Any:
    """POST the project configuration fetch-values endpoint."""
    body = model("FetchValuesBody").from_dict({"keys": keys})
    return operation("project_configuration", "fetch_values").sync_detailed(
        client=api_client, body=body, **kwargs
    )


def _merge_values(target: Target) -> Handler:
    """Make get-configuration always return values.

    The GET configuration endpoint returns keys without values; values
    come from a separate POST. We always make both calls and emit the
    merged ``list[ConfigKeyValueResponse]`` (values in plaintext).
    """
    get_configuration = operation("project_configuration", "get_configuration")
    parameters = api_parameters(get_configuration.sync_detailed)

    def handler(namespace: argparse.Namespace) -> None:
        kwargs = api_kwargs(namespace, parameters)
        with connect(target) as api_client:
            keys = parsed_list(
                get_configuration.sync_detailed(client=api_client, **kwargs)
            )
            if keys is None:
                return
            emit(
                _fetch_values(
                    api_client, [item.key for item in keys], **kwargs
                )
            )

    return handler


def _config_copy_handler(target: Target, *, rename: bool) -> Handler:
    """The handler for the synthetic copy/rename commands."""

    def handler(namespace: argparse.Namespace) -> None:
        if rename and namespace.key == namespace.new_key:
            fail("key and new-key must be different")
        scope: dict[str, typing.Any] = {}
        if namespace.source is not None:
            scope["source"] = namespace.source
        if namespace.environment is not None:
            scope["environment"] = namespace.environment
        ids = {
            "org_slug": namespace.org_slug,
            "project_id": namespace.project_id,
        }
        with connect(target) as api_client:
            fetched = parsed_list(
                _fetch_values(api_client, [namespace.key], **ids, **scope)
            )
            if fetched is None:
                return
            match = next(
                (item for item in fetched if item.key == namespace.key), None
            )
            if match is None:
                fail(f"key not found: {namespace.key}", code=1)
            body = model("ConfigValue")(
                data_type=match.data_type,
                value=match.value,
                secret=match.secret,
            )
            result = operation(
                "project_configuration", "set_configuration_value"
            ).sync_detailed(
                client=api_client,
                key=namespace.new_key,
                body=body,
                **ids,
                **scope,
            )
            if rename and result.status_code < 400:
                deleted = operation(
                    "project_configuration", "delete_configuration_key"
                ).sync_detailed(
                    client=api_client, key=namespace.key, **ids, **scope
                )
                if deleted.status_code >= 400:
                    print(
                        f"Value written to {namespace.new_key!r}, but "
                        f"deleting {namespace.key!r} failed; both keys "
                        f"now exist.",
                        file=sys.stderr,
                    )
                    emit(deleted)
                    return
            emit(result)

    return handler


def _config_copy_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    target: Target,
    organization: str | None,
    *,
    rename: bool,
) -> None:
    """A synthetic copy/rename command for project configuration.

    Neither operation has a dedicated endpoint. We fetch the source
    key's value, set it on the destination key, and (for rename) delete
    the source key. All steps share the same
    ``--source``/``--environment`` scope.
    """
    verb = "Rename" if rename else "Copy"
    help_text = f"{verb} a configuration key to NEW_KEY."
    if rename:
        help_text += (
            "\n\nNot atomic: the value is written to NEW_KEY, then KEY is "
            "deleted. If the delete fails, both keys will exist."
        )
    parser = subparsers.add_parser(
        name,
        help=short_help(help_text, name),
        description=help_text,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("key", help="key")
    parser.add_argument("new_key", metavar="new-key", help="new_key")
    add_org_slug(parser, organization)
    parser.add_argument(
        "--project-id", dest="project_id", required=True, help="project_id"
    )
    parser.add_argument("--source", default=None, help="source")
    parser.add_argument("--environment", default=None, help="environment")
    parser.set_defaults(handler=_config_copy_handler(target, rename=rename))


# parameter name -> the add_argument call that replaces the generic one
FIELD_OVERRIDES: dict[
    str,
    collections.abc.Callable[[argparse.ArgumentParser, str | None], None],
] = {"org_slug": add_org_slug}

# (tag, operation) pairs the tree leaves out
HIDDEN: frozenset[tuple[str, str]] = frozenset(
    {("project_configuration", "fetch_values")}
)

# (tag, operation) -> the name the command is invoked by
RENAMES: dict[tuple[str, str], str] = {
    ("project_configuration", "get_configuration"): "get",
    ("project_configuration", "set_configuration_value"): "set",
    ("project_configuration", "delete_configuration_key"): "delete",
}

# (tag, operation) -> a replacement handler for the built parser
AUGMENTS: dict[
    tuple[str, str], collections.abc.Callable[[Target], Handler]
] = {("project_configuration", "get_configuration"): _merge_values}

# tag -> command name -> a parser no operation backs
SYNTHETIC: dict[
    str,
    dict[
        str,
        collections.abc.Callable[
            [argparse._SubParsersAction, str, Target, str | None], None
        ],
    ],
] = {
    "project_configuration": {
        "copy": lambda subparsers, name, target, organization: (
            _config_copy_parser(
                subparsers, name, target, organization, rename=False
            )
        ),
        "rename": lambda subparsers, name, target, organization: (
            _config_copy_parser(
                subparsers, name, target, organization, rename=True
            )
        ),
    }
}


def spec(settings: Settings) -> str:
    """The spec text, raising ``SpecUnavailable`` when there is none.

    A missing source, an unreachable instance, and an unreadable file
    all raise rather than exiting: help has to work anyway.
    """
    if settings.spec_file is not None:
        try:
            return settings.spec_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SpecUnavailable(
                f"could not read the OpenAPI spec from "
                f"{settings.spec_file}: {exc}"
            ) from exc
    if settings.url is not None:
        try:
            return fetch_spec(settings.target())
        except httpx.HTTPError as exc:
            raise SpecUnavailable(
                f"could not fetch the OpenAPI spec from "
                f"{settings.url}: {exc!r}"
            ) from exc
    raise SpecUnavailable(
        "no OpenAPI spec: set --url (IMBI_URL) to an Imbi instance, "
        "or --spec-file (IMBI_SPEC_FILE) to a saved spec"
    )


def spec_version(text: str) -> str | None:
    """The spec's ``info.version``, when the text is a JSON spec."""
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    info = document.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return version if isinstance(version, str) else None


def installed_version() -> str | None:
    """The installed client distribution's version, when there is one."""
    try:
        return importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def bind_server_version(version: str | None) -> None:
    """Bind the Imbi version, keeping one that is already bound."""
    global SERVER_VERSION
    if SERVER_VERSION is None and version:
        SERVER_VERSION = version


def resolve_server_version(settings: Settings) -> None:
    """Learn the Imbi version without rendering a client.

    A baked-in value wins, then an installed client's distribution
    metadata, then the spec text itself. Costs at most one file read or
    one HTTP GET, never raises, and leaves ``SERVER_VERSION`` None when
    no source names a version.
    """
    if SERVER_VERSION is not None:
        return
    bind_server_version(installed_version())
    if SERVER_VERSION is not None:
        return
    try:
        text = spec(settings)
    except SpecUnavailable:
        return
    bind_server_version(spec_version(text))


def generator() -> types.ModuleType:
    """The generator package, or why a client cannot be rendered."""
    try:
        from imbi_cli import generator  # noqa: PLC0415
    except ImportError as exc:
        raise SpecUnavailable(
            "generating an API client needs openapi-python-client: "
            "install imbi-cli[generator], or run an archive built with "
            "build-pyz"
        ) from exc
    return generator


BUILD_PYZ = "build-pyz"
_BUILD_PYZ_HELP = (
    "Generate an executable .pyz archive holding the API client and the CLI"
)


def add_build_pyz_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "output",
        type=pathlib.Path,
        nargs="?",
        default=None,
        help="Path of the .pyz archive to write (default: "
        f"{PROG_NAME}_v<server version>.pyz)",
    )


# semver.org's grammar: major.minor.patch, optional pre-release and build
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def default_archive_name(server_version: str | None) -> pathlib.Path:
    """The archive name for the server version in hand.

    The version is named as ``v<semver>``; one that is missing or not
    semver is named ``unknown``.
    """
    version = (server_version or "").removeprefix("v")
    if not _SEMVER.fullmatch(version):
        return pathlib.Path(f"{PROG_NAME}_unknown.pyz")
    return pathlib.Path(f"{PROG_NAME}_v{version}.pyz")


def build_pyz(settings: Settings, output: pathlib.Path | None) -> None:
    """Render the spec into an executable archive at ``output``.

    Without ``output`` the archive is written to the working directory,
    named for the spec's Imbi version.
    """
    try:
        text = spec(settings)
        if output is None:
            output = default_archive_name(spec_version(text))
        generator().generate(text, output)
    except (SpecUnavailable, SpecError) as exc:
        fail(str(exc), code=1)
    print(output)


def _build_pyz_handler(settings: Settings) -> Handler:
    def handler(namespace: argparse.Namespace) -> None:
        build_pyz(settings, namespace.output)

    return handler


def acquire_client(settings: Settings) -> None:
    """Bind the generated API client, however it is available.

    An importable package wins -- an archive carries its own -- and
    last the spec, rendered into memory.

    Raises ``SpecUnavailable`` or ``SpecError``.
    """
    if _root is not None:
        return
    if client_available():
        bind_server_version(installed_version())
        bind_client(importlib.import_module(PACKAGE_NAME))
        return
    text = spec(settings)
    bind_server_version(spec_version(text))
    generator().render(text)


def parser(settings: Settings) -> argparse.ArgumentParser:
    """The full parser, with one subparser per tag."""
    root = argparse.ArgumentParser(prog=PROG_NAME, description=description())
    add_global_options(root)
    subparsers = root.add_subparsers(dest="tag", metavar="TAG")
    target = settings.target()
    if not ARCHIVED:
        pyz = subparsers.add_parser(BUILD_PYZ, help=_BUILD_PYZ_HELP)
        add_build_pyz_arguments(pyz)
        pyz.set_defaults(handler=_build_pyz_handler(settings))
    for tag in tags():
        tag_parser(subparsers, tag, target, settings.organization)
    return root


def unavailable_parser(reason: str) -> argparse.ArgumentParser:
    """A parser whose every command reports why there is no client."""
    root = argparse.ArgumentParser(prog=PROG_NAME, description=description())
    add_global_options(root)
    root.add_argument(
        "command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
    )

    def handler(namespace: argparse.Namespace) -> None:
        fail(reason, code=1)

    root.set_defaults(handler=handler)
    return root


def run(args: collections.abc.Sequence[str] | None = None) -> int:
    """argv in, exit code out."""
    argv = list(sys.argv[1:] if args is None else args)
    globals_only = argparse.ArgumentParser(prog=PROG_NAME, add_help=False)
    add_global_options(globals_only)
    namespace, rest = globals_only.parse_known_args(argv)
    load_env(namespace.env_file)
    settings = resolve(namespace)
    init_logging(settings.verbose)
    if namespace.version:
        resolve_server_version(settings)
        print(version_line())
        return 0
    # build-pyz needs the spec and the generator, never a bound client,
    # so it is handled before the client is acquired.
    if not ARCHIVED and rest[:1] == [BUILD_PYZ]:
        sub = argparse.ArgumentParser(
            prog=f"{PROG_NAME} {BUILD_PYZ}", description=_BUILD_PYZ_HELP
        )
        add_build_pyz_arguments(sub)
        build_pyz(settings, sub.parse_args(rest[1:]).output)
        return 0
    root: argparse.ArgumentParser
    try:
        acquire_client(settings)
    except (SpecUnavailable, SpecError) as exc:
        root = unavailable_parser(str(exc))
    else:
        root = parser(settings)
    parsed = root.parse_args(argv)
    handler = getattr(parsed, "handler", None)
    if handler is None:
        root.print_help(sys.stderr)
        return USAGE_EXIT
    handler(parsed)
    return 0


def main() -> None:
    sys.exit(run())
