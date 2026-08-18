"""The spec transforms, against documents built in the test."""

import json

import pytest

from imbi_cli.generator import openapi


def document(*operations: tuple[str, dict]) -> dict:
    """A document whose paths hold the given (path, operation) pairs."""
    return {
        "openapi": "3.1.0",
        "paths": {path: {"get": operation} for path, operation in operations},
    }


def test_operation_ids_lose_the_path_suffix() -> None:
    spec = document(("/a", {"operationId": "get_project_api_projects__get"}))
    openapi.shorten_operation_ids(spec)
    assert spec["paths"]["/a"]["get"]["operationId"] == "get_project"


def test_colliding_operation_ids_gain_the_tag() -> None:
    spec = document(
        ("/a", {"operationId": "list_api_a__get", "tags": ["Projects"]}),
        ("/b", {"operationId": "list_api_b__get", "tags": ["Tag: Teams"]}),
    )
    openapi.shorten_operation_ids(spec)
    assert spec["paths"]["/a"]["get"]["operationId"] == "list_projects"
    assert spec["paths"]["/b"]["get"]["operationId"] == "list_tag_teams"


def test_colliding_ids_under_the_same_tag_gain_the_method_and_path() -> None:
    spec = document(
        ("/a", {"operationId": "list_api_a__get", "tags": ["Projects"]}),
        ("/b", {"operationId": "list_api_b__get", "tags": ["Projects"]}),
    )
    openapi.shorten_operation_ids(spec)
    assert spec["paths"]["/a"]["get"]["operationId"] == "list_projects_get_a"
    assert spec["paths"]["/b"]["get"]["operationId"] == "list_projects_get_b"


def test_colliding_untagged_operation_ids_stay_unique() -> None:
    spec = document(
        ("/a", {"operationId": "list_api_a__get"}),
        ("/b", {"operationId": "list_api_b__get", "tags": []}),
    )
    openapi.shorten_operation_ids(spec)
    assert spec["paths"]["/a"]["get"]["operationId"] == "list_untagged_get_a"
    assert spec["paths"]["/b"]["get"]["operationId"] == "list_untagged_get_b"


def test_a_document_without_paths_yields_no_operations() -> None:
    assert openapi.normalize(json.dumps({"openapi": "3.1.0"})) == {
        "openapi": "3.1.0"
    }


def test_the_most_specific_tag_wins() -> None:
    spec = document(("/a", {"tags": ["Organizations", "Projects"]}))
    openapi.use_most_specific_tag(spec)
    assert spec["paths"]["/a"]["get"]["tags"] == ["Projects"]


def test_nullable_path_params_collapse() -> None:
    spec = document(
        (
            "/a",
            {
                "parameters": [
                    {
                        "in": "path",
                        "name": "project_id",
                        "schema": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "title": "Project Id",
                        },
                    }
                ]
            },
        )
    )
    openapi.denullify_path_params(spec)
    (parameter,) = spec["paths"]["/a"]["get"]["parameters"]
    assert parameter["schema"] == {"type": "string", "title": "Project Id"}


def test_component_schema_titles_are_dropped() -> None:
    spec = document()
    spec["components"] = {
        "schemas": {
            "Blueprint-Input": {"title": "Blueprint", "type": "object"}
        }
    }
    openapi.detitle_component_schemas(spec)
    assert spec["components"]["schemas"]["Blueprint-Input"] == {
        "type": "object"
    }


def test_a_malformed_document_raises_json_decode_error() -> None:
    with pytest.raises(json.JSONDecodeError):
        openapi.normalize("not json")


def test_normalize_applies_every_transform() -> None:
    spec = json.dumps(
        document(
            (
                "/a",
                {
                    "operationId": "list_projects_api_a__get",
                    "tags": ["Organizations", "Projects"],
                },
            )
        )
    )
    normalized = openapi.normalize(spec)
    operation = normalized["paths"]["/a"]["get"]
    assert operation["operationId"] == "list_projects"
    assert operation["tags"] == ["Projects"]
