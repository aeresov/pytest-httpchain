"""Whole-document scenarios combining external refs, internal refs and merging."""

from pytest_httpchain.jsonref.loader import load_json


def test_external_refs_with_internal_ref_siblings(create_json_files):
    files = create_json_files(
        {
            "main.json": {
                "base_config": {"timeout": 30, "retries": 3},
                "endpoints": {
                    "users": {"$ref": "endpoints/users.json", "config": {"$ref": "#/base_config"}},
                    "products": {"$ref": "endpoints/products.json", "config": {"$ref": "#/base_config"}},
                },
            },
            "endpoints/users.json": {"path": "/api/users", "methods": ["GET", "POST"]},
            "endpoints/products.json": {"path": "/api/products", "methods": ["GET"]},
        }
    )
    config = {"timeout": 30, "retries": 3}
    assert load_json(files["main.json"])["endpoints"] == {
        "users": {"path": "/api/users", "methods": ["GET", "POST"], "config": config},
        "products": {"path": "/api/products", "methods": ["GET"], "config": config},
    }


def test_refs_with_siblings_at_each_nesting_level(create_json_files):
    files = create_json_files(
        {
            "main.json": {"level1": {"$ref": "level1.json", "added_at_1": "one"}},
            "level1.json": {"level2": {"$ref": "level2.json", "added_at_2": "two"}},
            "level2.json": {"level3": {"value": "deep"}},
        }
    )
    assert load_json(files["main.json"]) == {
        "level1": {"added_at_1": "one", "level2": {"added_at_2": "two", "level3": {"value": "deep"}}},
    }


def test_array_elements_extend_a_shared_template(create_json_files):
    files = create_json_files(
        {
            "main.json": {
                "template": {"type": "item", "active": True},
                "items": [
                    {"$ref": "#/template", "name": "first"},
                    {"$ref": "#/template", "name": "second"},
                    {"$ref": "#/template", "name": "third", "priority": 1},
                ],
            },
        }
    )
    assert load_json(files["main.json"])["items"] == [
        {"type": "item", "active": True, "name": "first"},
        {"type": "item", "active": True, "name": "second"},
        {"type": "item", "active": True, "name": "third", "priority": 1},
    ]


def test_array_of_external_refs_with_internal_ref_siblings(create_json_files):
    files = create_json_files(
        {
            "main.json": {
                "defaults": {"enabled": True},
                "features": [
                    {"$ref": "features/feature1.json", "settings": {"$ref": "#/defaults"}},
                    {"$ref": "features/feature2.json", "settings": {"$ref": "#/defaults", "timeout": 30}},
                ],
            },
            "features/feature1.json": {"name": "Feature One", "version": 1},
            "features/feature2.json": {"name": "Feature Two", "version": 2},
        }
    )
    assert load_json(files["main.json"])["features"] == [
        {"name": "Feature One", "version": 1, "settings": {"enabled": True}},
        {"name": "Feature Two", "version": 2, "settings": {"enabled": True, "timeout": 30}},
    ]


def test_diamond_dependency(create_json_files):
    """A refs B and C, both ref D: a shared dependency is not a cycle."""
    files = create_json_files(
        {
            "a.json": {"b_data": {"$ref": "b.json"}, "c_data": {"$ref": "c.json"}},
            "b.json": {"source": "B", "shared": {"$ref": "d.json#/value"}},
            "c.json": {"source": "C", "shared": {"$ref": "d.json#/value"}},
            "d.json": {"value": "from-D"},
        }
    )
    assert load_json(files["a.json"]) == {
        "b_data": {"source": "B", "shared": "from-D"},
        "c_data": {"source": "C", "shared": "from-D"},
    }


def test_siblings_deep_merge_into_nested_dicts(create_json_files):
    """Sibling keys are added at every depth, never overriding."""
    files = create_json_files(
        {
            "main.json": {
                "config": {
                    "$ref": "base.json",
                    "database": {"pool": {"idle_timeout": 300}},
                    "logging": {"output": "stdout"},
                },
            },
            "base.json": {
                "database": {"host": "localhost", "port": 5432, "pool": {"min": 5, "max": 10}},
                "logging": {"format": "json", "level": "info"},
            },
        }
    )
    assert load_json(files["main.json"])["config"] == {
        "database": {"host": "localhost", "port": 5432, "pool": {"min": 5, "max": 10, "idle_timeout": 300}},
        "logging": {"format": "json", "level": "info", "output": "stdout"},
    }
