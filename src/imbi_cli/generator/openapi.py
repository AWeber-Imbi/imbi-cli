"""Spec transforms for the Imbi OpenAPI document.

Normalizes the FastAPI-generated spec so generated model and module
names are stable and collision-free. Text in, document out: nothing here
reads the environment or opens a socket, and the document is never
re-serialized.
"""

import collections
import collections.abc
import json
import re

from imbi_cli import app

HTTP_METHODS = {
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
}


def _keyed_operations(
    spec: dict,
) -> collections.abc.Iterator[tuple[str, str, dict]]:
    """Every (path, method, operation) in the document, in path order.

    A document without a ``paths`` mapping simply yields nothing: a
    malformed document must still let ``--help`` explain itself.
    """
    for path, methods in (spec.get("paths") or {}).items():
        for method, operation in (methods or {}).items():
            if method in HTTP_METHODS:
                yield path, method, operation


def _operations(spec: dict) -> collections.abc.Iterator[dict]:
    """Every operation object in the document, in path order."""
    for _path, _method, operation in _keyed_operations(spec):
        yield operation


def _identifier(text: str) -> str:
    """The text reduced to a snake_case identifier fragment."""
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()


def shorten_operation_ids(spec: dict) -> None:
    """Strip the FastAPI path suffix from operationIds.

    FastAPI generates operationIds like
    ``get_project_api_organizations__org_slug__projects__project_id__get``.
    Everything from ``_api_`` onward duplicates the path and produces
    filesystem-breaking module names, so keep only the function-name
    prefix. Collisions (same prefix under different tags) get the tag
    appended; ones the tag cannot split -- the same or no tag -- get the
    method and path appended, which the document keys by, so the result
    is unique.

    Raises ``SpecError`` if a shortened id still collides.
    """
    operations = []
    counts: collections.Counter[str] = collections.Counter()
    for path, method, operation in _keyed_operations(spec):
        operation_id = operation.get("operationId")
        if not operation_id:
            continue
        short = operation_id.split("_api_")[0]
        operations.append((path, method, operation, short))
        counts[short] += 1
    tagged = []
    for path, method, operation, short in operations:
        if counts[short] > 1:
            tag = (operation.get("tags") or ["untagged"])[0]
            short = "_".join([short, _identifier(tag)])
        tagged.append((path, method, operation, short))
    tagged_counts = collections.Counter(short for *_, short in tagged)
    seen: set[str] = set()
    for path, method, operation, short in tagged:
        if tagged_counts[short] > 1:
            short = "_".join([short, _identifier(f"{method} {path}")])
        if short in seen:
            raise app.SpecError(f"operationId {short!r} is not unique")
        seen.add(short)
        operation["operationId"] = short


def use_most_specific_tag(spec: dict) -> None:
    """Keep only the most specific tag on each operation.

    The spec tags operations like ``["Organizations", "Projects"]`` but
    openapi-python-client groups by the first tag only, which would dump
    129 operations into one ``organizations`` package. The last tag is
    the most specific, so promote it.
    """
    for operation in _operations(spec):
        tags = operation.get("tags")
        if tags:
            operation["tags"] = [tags[-1]]


def denullify_path_params(spec: dict) -> None:
    """Collapse ``anyOf: [T, null]`` path parameters to ``T``.

    FastAPI emits some path parameters (e.g. ``project_id``) as
    ``anyOf: [{type: string}, {type: null}]``. openapi-python-client
    rejects nullable path parameters ("None | str is not allowed in
    path") and silently drops the whole endpoint. Path parameters are
    always present, so the null branch is spurious: keep the single
    non-null branch's schema.
    """
    for operation in _operations(spec):
        for parameter in operation.get("parameters", []):
            if parameter.get("in") != "path":
                continue
            schema = parameter.get("schema", {})
            branches = [
                branch
                for branch in schema.get("anyOf", [])
                if branch.get("type") != "null"
            ]
            if "anyOf" in schema and len(branches) == 1:
                title = schema.get("title")
                schema = dict(branches[0])
                if title and "title" not in schema:
                    schema["title"] = title
                parameter["schema"] = schema


def detitle_component_schemas(spec: dict) -> None:
    """Remove titles from top-level component schemas.

    FastAPI emits ``Blueprint-Input``/``Blueprint-Output`` schema pairs
    that both carry ``title: Blueprint``. openapi-python-client names
    models from the title when present, so the pair collides and both
    models (plus everything referencing them) get dropped. Removing the
    title makes the generator fall back to the unique schema key.
    """
    for schema in spec.get("components", {}).get("schemas", {}).values():
        schema.pop("title", None)


def normalize(text: str) -> dict:
    """Deserialize the raw Imbi OpenAPI document and transform it."""
    spec = json.loads(text)
    shorten_operation_ids(spec)
    use_most_specific_tag(spec)
    denullify_path_params(spec)
    detitle_component_schemas(spec)
    return spec
