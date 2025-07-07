from http import HTTPStatus

import pytest
from pydantic import ValidationError

from pytest_http.models import Stage, Verify


@pytest.mark.parametrize(
    "status_input,expected_status,description",
    [
        (HTTPStatus.OK, HTTPStatus.OK, "with_status"),
        (None, None, "with_none"),
        ("no_args", None, "without_args"),
    ],
)
def test_verify_model_status_handling(status_input, expected_status, description):
    if description == "without_args":
        verify = Verify()
    else:
        verify = Verify(status=status_input)
    assert verify.status == expected_status


def test_verify_model_with_different_status_codes():
    status_codes = [
        HTTPStatus.OK,
        HTTPStatus.CREATED,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.INTERNAL_SERVER_ERROR,
    ]

    for status_code in status_codes:
        verify = Verify(status=status_code)
        assert verify.status == status_code


def test_verify_model_with_integer_status():
    verify = Verify(status=200)
    assert verify.status == HTTPStatus.OK
    assert verify.status.value == 200


def test_verify_model_invalid_status():
    with pytest.raises(ValidationError):
        Verify(status=999)  # Invalid HTTP status code


@pytest.mark.parametrize(
    "verify_input,expected_verify_exists,expected_status",
    [
        (Verify(status=HTTPStatus.OK), True, HTTPStatus.OK),
        ({"status": 200}, True, HTTPStatus.OK),
        (None, False, None),
        ({}, True, None),
        ("no_verify", False, None),
    ],
)
def test_stage_verify_field_handling(verify_input, expected_verify_exists, expected_status):
    if verify_input == "no_verify":
        stage = Stage(name="test_stage")
    else:
        stage = Stage(name="test_stage", verify=verify_input)

    if expected_verify_exists:
        assert stage.verify is not None
        assert stage.verify.status == expected_status
    else:
        assert stage.verify is None


def test_stage_verify_field_optional():
    stage_data = {"name": "test_stage"}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is None


def test_stage_with_complete_verify_data():
    stage_data = {"name": "test_stage", "url": "https://api.example.com/test", "verify": {"status": 201}}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.CREATED
