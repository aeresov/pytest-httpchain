"""MessagePack bytes can move from a response into a later URL parameter."""

from tests.integration.helpers import stage


def test_msgpack_body_save_and_binary_parameter(run_scenario):
    scenario = {
        "stages": [
            stage(
                "send",
                "/echo/msgpack",
                request={"method": "POST", "body": {"msgpack": {"name": "device", "b": "{{ hex_bytes('00ff80') }}"}}},
                response=[
                    {"verify": {"status": 200, "jmespath": {"name": "device", "b": "{{ hex_bytes('00ff80') }}"}}},
                    {"save": {"jmespath": {"packet": "@", "binary": "b"}}},
                ],
            ),
            stage(
                "query",
                "/echo/msgpack-param",
                request={"params": {"packet": "{{ msgpack_pack(packet) }}"}},
                response=[{"verify": {"status": 200, "jmespath": {"b": "{{ binary }}", "name": "device"}}}],
            ),
        ]
    }
    run_scenario(scenario).assert_outcomes(passed=2)


def test_response_codec_override(run_scenario):
    scenario = {
        "stages": [
            stage(
                "mislabeled",
                "/echo/msgpack?wrong_type=1",
                request={"method": "POST", "headers": {"Content-Type": "application/msgpack"}, "body": {"bytes": "{{ msgpack_pack({'id': 42}) }}"}},
                response=[{"verify": {"jmespath": {"id": 42}, "body": {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}}}],
                response_codec="msgpack",
            )
        ]
    }
    run_scenario(scenario).assert_outcomes(passed=1)
