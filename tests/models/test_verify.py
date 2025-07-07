from http import HTTPStatus

import pytest
from pydantic import ValidationError

from pytest_http.models import Stage, Verify


def test_verify_model_with_status():
    verify = Verify(status=HTTPStatus.OK)
    assert verify.status == HTTPStatus.OK


def test_verify_model_without_status():
    verify = Verify()
    assert verify.status is None


def test_verify_model_with_none_status():
    verify = Verify(status=None)
    assert verify.status is None


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


def test_stage_with_verify_field():
    stage = Stage(name="test_stage", data={}, verify=Verify(status=HTTPStatus.OK))
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.OK


def test_stage_with_verify_dict():
    stage = Stage(name="test_stage", data={}, verify={"status": 200})
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.OK


def test_stage_without_verify():
    stage = Stage(name="test_stage", data={})
    assert stage.verify is None


def test_stage_with_none_verify():
    stage = Stage(name="test_stage", data={}, verify=None)
    assert stage.verify is None


def test_stage_with_empty_verify():
    stage = Stage(name="test_stage", data={}, verify={})
    assert stage.verify is not None
    assert stage.verify.status is None


def test_stage_verify_field_optional():
    stage_data = {"name": "test_stage", "data": {}}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is None


def test_stage_with_complete_verify_data():
    stage_data = {"name": "test_stage", "data": {}, "url": "https://api.example.com/test", "verify": {"status": 201}}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.CREATED
