"""Integration tests combining multiple features."""

from pytest_httpchain_jsonref.loader import load_json


class TestIntegration:
    """Integration tests for complex scenarios combining multiple features."""

    def test_complex_resolution_with_external_and_internal_refs(self, create_json_files):
        """Test combining external file refs, internal refs, and merging."""
        files = create_json_files(
            {
                "main.json": {
                    "base_config": {"timeout": 30, "retries": 3},
                    "endpoints": {
                        "users": {
                            "$ref": "endpoints/users.json",
                            "config": {"$ref": "#/base_config"},
                        },
                        "products": {
                            "$ref": "endpoints/products.json",
                            "config": {"$ref": "#/base_config"},
                        },
                    },
                },
                "endpoints/users.json": {
                    "path": "/api/users",
                    "methods": ["GET", "POST"],
                },
                "endpoints/products.json": {
                    "path": "/api/products",
                    "methods": ["GET"],
                },
            }
        )
        result = load_json(files["main.json"])

        # Check external refs resolved
        assert result["endpoints"]["users"]["path"] == "/api/users"
        assert result["endpoints"]["products"]["path"] == "/api/products"

        # Check internal refs resolved and merged
        assert result["endpoints"]["users"]["config"]["timeout"] == 30
        assert result["endpoints"]["products"]["config"]["retries"] == 3

        # Check no $ref remains
        assert "$ref" not in result["endpoints"]["users"]
        assert "$ref" not in result["endpoints"]["products"]

    def test_chained_external_references(self, create_json_files):
        """Test chain of external file references: A → B → C."""
        files = create_json_files(
            {
                "a.json": {"data": {"$ref": "b.json#/nested"}},
                "b.json": {"nested": {"$ref": "c.json#/value"}},
                "c.json": {"value": {"final": "result"}},
            }
        )
        result = load_json(files["a.json"])
        assert result["data"]["final"] == "result"

    def test_deep_nesting_with_refs_at_each_level(self, create_json_files):
        """Test refs at multiple nesting levels."""
        files = create_json_files(
            {
                "main.json": {
                    "level1": {
                        "$ref": "level1.json",
                        "added_at_1": "one",
                    },
                },
                "level1.json": {
                    "level2": {
                        "$ref": "level2.json",
                        "added_at_2": "two",
                    },
                },
                "level2.json": {
                    "level3": {
                        "value": "deep",
                    },
                },
            }
        )
        result = load_json(files["main.json"])
        assert result["level1"]["added_at_1"] == "one"
        assert result["level1"]["level2"]["added_at_2"] == "two"
        assert result["level1"]["level2"]["level3"]["value"] == "deep"

    def test_array_with_refs_to_shared_template(self, create_json_files):
        """Test array elements referencing a shared template with additions."""
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
        result = load_json(files["main.json"])

        assert len(result["items"]) == 3
        assert all(item["type"] == "item" for item in result["items"])
        assert all(item["active"] is True for item in result["items"])
        assert result["items"][0]["name"] == "first"
        assert result["items"][2]["priority"] == 1

    def test_mixed_internal_and_external_with_arrays(self, create_json_files):
        """Test complex scenario with arrays, internal and external refs."""
        files = create_json_files(
            {
                "main.json": {
                    "defaults": {"enabled": True},
                    "features": [
                        {
                            "$ref": "features/feature1.json",
                            "settings": {"$ref": "#/defaults"},
                        },
                        {
                            "$ref": "features/feature2.json",
                            "settings": {"$ref": "#/defaults", "timeout": 30},
                        },
                    ],
                },
                "features/feature1.json": {"name": "Feature One", "version": 1},
                "features/feature2.json": {"name": "Feature Two", "version": 2},
            }
        )
        result = load_json(files["main.json"])

        assert result["features"][0]["name"] == "Feature One"
        assert result["features"][0]["settings"]["enabled"] is True
        assert result["features"][1]["name"] == "Feature Two"
        assert result["features"][1]["settings"]["enabled"] is True
        assert result["features"][1]["settings"]["timeout"] == 30

    def test_ref_to_array_element_in_external_file(self, create_json_files):
        """Test referencing specific array element in external file."""
        files = create_json_files(
            {
                "main.json": {
                    "selected": {"$ref": "data.json#/items/1"},
                },
                "data.json": {
                    "items": [
                        {"id": 1, "name": "first"},
                        {"id": 2, "name": "second"},
                        {"id": 3, "name": "third"},
                    ],
                },
            }
        )
        result = load_json(files["main.json"])
        assert result["selected"]["id"] == 2
        assert result["selected"]["name"] == "second"

    def test_json_pointer_special_chars_with_external_ref(self, create_json_files):
        """Test JSON pointer escape sequences in external file refs."""
        files = create_json_files(
            {
                "main.json": {
                    "ref": {"$ref": "data.json#/a~1b"},
                },
                "data.json": {
                    "a/b": "slash-in-key",
                },
            }
        )
        result = load_json(files["main.json"])
        assert result["ref"] == "slash-in-key"

    def test_subdirectory_refs_with_back_traversal(self, create_json_files):
        """Test references between subdirectories using parent traversal."""
        files = create_json_files(
            {
                "shared/common.json": {"shared_value": 42},
                "services/api/config.json": {
                    "common": {"$ref": "../../shared/common.json"},
                    "endpoint": "/api",
                },
            }
        )
        result = load_json(files["services/api/config.json"])
        assert result["common"]["shared_value"] == 42
        assert result["endpoint"] == "/api"

    def test_diamond_dependency(self, create_json_files):
        """Test diamond dependency pattern: A refs B and C, both ref D."""
        files = create_json_files(
            {
                "a.json": {
                    "b_data": {"$ref": "b.json"},
                    "c_data": {"$ref": "c.json"},
                },
                "b.json": {"source": "B", "shared": {"$ref": "d.json#/value"}},
                "c.json": {"source": "C", "shared": {"$ref": "d.json#/value"}},
                "d.json": {"value": "from-D"},
            }
        )
        result = load_json(files["a.json"])
        assert result["b_data"]["source"] == "B"
        assert result["b_data"]["shared"] == "from-D"
        assert result["c_data"]["source"] == "C"
        assert result["c_data"]["shared"] == "from-D"

    def test_nested_merge_deep_structures(self, create_json_files):
        """Test deep merge of nested structures with additions (not overrides)."""
        files = create_json_files(
            {
                "main.json": {
                    "config": {
                        "$ref": "base.json",
                        "database": {
                            "pool": {"idle_timeout": 300},
                        },
                        "logging": {
                            "output": "stdout",
                        },
                    },
                },
                "base.json": {
                    "database": {
                        "host": "localhost",
                        "port": 5432,
                        "pool": {"min": 5, "max": 10},
                    },
                    "logging": {
                        "format": "json",
                        "level": "info",
                    },
                },
            }
        )
        result = load_json(files["main.json"])

        # Check base values preserved
        assert result["config"]["database"]["host"] == "localhost"
        assert result["config"]["database"]["port"] == 5432
        assert result["config"]["logging"]["format"] == "json"
        assert result["config"]["logging"]["level"] == "info"

        # Check deep merge of pool - new key added
        assert result["config"]["database"]["pool"]["min"] == 5
        assert result["config"]["database"]["pool"]["max"] == 10
        assert result["config"]["database"]["pool"]["idle_timeout"] == 300

        # Check logging output added
        assert result["config"]["logging"]["output"] == "stdout"
