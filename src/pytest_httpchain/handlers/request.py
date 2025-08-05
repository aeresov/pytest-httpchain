import logging
from contextlib import ExitStack
from typing import Any

import pytest_httpchain_engine.models.entities
import requests
from pytest_httpchain_engine.functions import AuthFunction
from pytest_httpchain_engine.models.entities import Request, UserFunctionKwargs, UserFunctionName

logger = logging.getLogger(__name__)


class RequestError(Exception):
    """An error making HTTP request."""


class RequestHandler:
    """Handles HTTP request preparation and execution."""

    @staticmethod
    def execute(session: requests.Session, model: Request) -> requests.Response:
        """Execute an HTTP request with the given model."""
        request_params = RequestHandler._prepare_request_params(model)

        if model.auth:
            request_params["auth"] = RequestHandler._configure_auth(model.auth)

        RequestHandler._add_body_to_params(model, request_params)
        return RequestHandler._execute_request(session, model, request_params)

    @staticmethod
    def _prepare_request_params(model: Request) -> dict[str, Any]:
        """Prepare basic request parameters."""
        request_params: dict[str, Any] = {
            "timeout": model.timeout,
            "allow_redirects": model.allow_redirects,
        }

        if model.params:
            request_params["params"] = model.params
        if model.headers:
            request_params["headers"] = model.headers

        if model.ssl:
            if model.ssl.verify is not None:
                request_params["verify"] = model.ssl.verify
            if model.ssl.cert is not None:
                request_params["cert"] = model.ssl.cert

        return request_params

    @staticmethod
    def _configure_auth(auth: UserFunctionKwargs | UserFunctionName) -> Any:
        """Configure authentication for the request."""
        try:
            match auth:
                case UserFunctionKwargs():
                    return AuthFunction.call_with_kwargs(auth.function.root, auth.kwargs)
                case UserFunctionName():
                    return AuthFunction.call(auth.root)
        except Exception as e:
            raise RequestError("Failed to configure stage authentication") from e

    @staticmethod
    def _add_body_to_params(model: Request, request_params: dict[str, Any]) -> None:
        """Add body data to request parameters."""
        match model.body:
            case None:
                pass
            case pytest_httpchain_engine.models.entities.JsonBody(json=data):
                request_params["json"] = data
            case pytest_httpchain_engine.models.entities.FormBody(form=data):
                request_params["data"] = data
            case pytest_httpchain_engine.models.entities.XmlBody(xml=data):
                request_params["data"] = data
            case pytest_httpchain_engine.models.entities.RawBody(raw=data):
                request_params["data"] = data
            case pytest_httpchain_engine.models.entities.FilesBody(files=data):
                request_params["files"] = data

    @staticmethod
    def _execute_request(session: requests.Session, model: Request, request_params: dict[str, Any]) -> requests.Response:
        """Execute the HTTP request with error handling."""
        with ExitStack() as stack:
            try:
                if "files" in request_params:
                    request_params["files"] = {field_name: stack.enter_context(open(file_path, "rb")) for field_name, file_path in request_params["files"]}

                return session.request(model.method.value, model.url, **request_params)

            except FileNotFoundError as e:
                raise RequestError("File not found for upload") from e
            except requests.Timeout as e:
                raise RequestError("HTTP request timed out") from e
            except requests.ConnectionError as e:
                raise RequestError("HTTP connection error") from e
            except requests.RequestException as e:
                raise RequestError("HTTP request failed") from e
            except Exception as e:
                raise RequestError("Unexpected error") from e
