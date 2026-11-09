"""Unit tests for the connection models: a scenario's connections, a stage's
step on one, and what the step waits for."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import ConnectionConfig, ConnectionExpect, ConnectionStep, JMESPathMatcher, Scenario, validate_rendered
from pytest_httpchain.models.types import RENDERED


class TestConnectionConfig:
    def test_defaults(self):
        config = ConnectionConfig.model_validate({"adapter": "adapters:LineJson"})
        assert (config.params, config.connect_timeout, config.inbox_limit) == ({}, 10.0, 1000)

    @pytest.mark.parametrize(
        ("adapter", "message"),
        [
            pytest.param("LineJson", r"Module path is required: use 'module:LineJson' format", id="bare-name"),
            pytest.param("adapters:Line-Json", r"Invalid adapter name format: adapters:Line-Json", id="bad-grammar"),
            # validate --deep imports what the name says, so it is never rendered.
            pytest.param("adapters:{{ cls }}", r"An adapter name is never rendered", id="template"),
        ],
    )
    def test_adapter_name(self, adapter, message):
        with pytest.raises(ValidationError, match=message):
            ConnectionConfig.model_validate({"adapter": adapter})

    def test_params_take_soft_keywords(self):
        """Constructor parameters are named as Python allows: `type` and
        `match` are common ones, which a template variable may not be."""
        config = ConnectionConfig.model_validate({"adapter": "m:C", "params": {"type": "v3", "match": 1, "_": 2}})
        assert set(config.params) == {"type", "match", "_"}

    @pytest.mark.parametrize("key", ["class", "not-an-id", "2fast"])
    def test_params_refuse_non_keyword_names(self, key):
        with pytest.raises(ValidationError, match=f"Invalid keyword argument name: '{key}'"):
            ConnectionConfig.model_validate({"adapter": "m:C", "params": {key: 1}})

    @pytest.mark.parametrize("value", [0, -1, True, float("inf")])
    def test_connect_timeout_is_positive_finite(self, value):
        with pytest.raises(ValidationError):
            ConnectionConfig.model_validate({"adapter": "m:C", "connect_timeout": value})

    def test_connect_timeout_template(self):
        assert ConnectionConfig.model_validate({"adapter": "m:C", "connect_timeout": "{{ t }}"}).connect_timeout == "{{ t }}"

    def test_scenario_names_connections_as_identifiers(self):
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            Scenario.model_validate({"connections": {"speaker-1": {"adapter": "m:C"}}})


class TestConnectionExpect:
    def test_match_expression(self):
        assert ConnectionExpect.model_validate({"match": "op == 'pong'"}).match == "op == 'pong'"

    def test_match_expectations_are_verify_jmespath(self):
        expect = ConnectionExpect.model_validate({"match": {"id": "{{ id }}", "items": {"length": 2}}})
        assert isinstance(expect.match, dict)
        assert expect.match["id"] == "{{ id }}"
        assert isinstance(expect.match["items"], JMESPathMatcher)

    @pytest.mark.parametrize(
        ("match", "message"),
        [
            # Never rendered: a template would be matched as text and never hold.
            pytest.param("id == '{{ id }}'", r"A match expression is never rendered, so it cannot hold '\{\{'", id="template"),
            pytest.param(r"id == '\{{ id }}'", r"cannot hold '\{\{'", id="escaped-template"),
            pytest.param("id ==", r"Invalid JMESPath expression", id="not-jmespath"),
            # An object is expectations: a literal object for equality goes under eq.
            pytest.param({"meta": {"page": 1}}, r"eq", id="literal-object"),
        ],
    )
    def test_match_refused(self, match, message):
        with pytest.raises(ValidationError, match=message):
            ConnectionExpect.model_validate({"match": match})

    def test_one_error_for_a_bad_match(self):
        """Told apart by type, so a bad expression is one error, not one per form."""
        with pytest.raises(ValidationError) as exc_info:
            ConnectionExpect.model_validate({"match": "id =="})
        assert exc_info.value.error_count() == 1

    @pytest.mark.parametrize("extra", [{"match": "a"}, {"absent": True}])
    def test_closed_takes_no_match_or_absent(self, extra):
        with pytest.raises(ValidationError, match="expect.closed waits for the connection to end, so it takes no match and no absent"):
            ConnectionExpect.model_validate({"closed": True, **extra})

    @pytest.mark.parametrize("value", [0, True, "abc"])
    def test_timeout_refused(self, value):
        with pytest.raises(ValidationError):
            ConnectionExpect.model_validate({"timeout": value})

    def test_timeout_refuses_bool_saying_so(self):
        with pytest.raises(ValidationError, match="A connection timeout is a number or a template, got true"):
            ConnectionExpect.model_validate({"timeout": True})


class TestConnectionStep:
    @pytest.mark.parametrize("action", [{"open": True}, {"drain": True}, {"close": True}, {"send": {}}, {"expect": {}}])
    def test_one_action_is_enough(self, action):
        ConnectionStep.model_validate({"name": "device", **action})

    def test_some_action_is_required(self):
        with pytest.raises(ValidationError, match="A connection step sets at least one of: open, drain, send, expect, close"):
            ConnectionStep.model_validate({"name": "device", "open": False})

    def test_send_takes_bytes_at_any_depth(self):
        """A binary protocol carries raw fields, which templates render as bytes."""
        step = validate_rendered(ConnectionStep, {"name": "device", "send": {"raw": b"\x00\x01", "frames": [{"body": b"\xff"}]}})
        assert step.send == {"raw": b"\x00\x01", "frames": [{"body": b"\xff"}]}

    @pytest.mark.parametrize("value", [(1, 2), {1, 2}, object()])
    def test_send_refuses_what_json_refuses(self, value):
        with pytest.raises(ValidationError):
            ConnectionStep.model_validate({"name": "device", "send": {"value": value}}, context=RENDERED)

    def test_send_hides_its_input_in_errors(self):
        """A rendered login message can carry a credential, which pydantic's
        input_value would print in the stage's failure."""
        with pytest.raises(ValidationError) as exc_info:
            ConnectionStep.model_validate({"name": "device", "send": {"password": ("s3cret",)}})
        assert "s3cret" not in str(exc_info.value)

    def test_send_template(self):
        assert ConnectionStep.model_validate({"name": "device", "send": "{{ message }}"}).send == "{{ message }}"

    @pytest.mark.parametrize("flag", ["open", "drain", "close"])
    def test_flags_are_not_templated(self, flag):
        """The validator reasons about a connection's lifecycle from them."""
        with pytest.raises(ValidationError):
            ConnectionStep.model_validate({"name": "device", flag: "{{ x }}"})
