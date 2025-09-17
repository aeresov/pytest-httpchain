"""Formatting utilities for test report generation.

This module provides functions to format HTTP requests and responses
for display in pytest test reports.
"""

import json

import curlify
import requests


def format_request(request: requests.PreparedRequest) -> str:
    return curlify.to_curl(request=request, pretty=True)


def format_response(response: requests.Response) -> str:
    lines = []

    # Status line
    if hasattr(response, "raw") and response.raw and hasattr(response.raw, "version"):
        lines.append(f"HTTP/{response.raw.version // 10}.{response.raw.version % 10} {response.status_code} {response.reason}")
    else:
        lines.append(f"HTTP/1.1 {response.status_code} {response.reason}")

    # Headers
    for key, value in response.headers.items():
        lines.append(f"{key}: {value}")

    # Empty line between headers and body
    lines.append("")

    # Body
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                lines.append(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, UnicodeDecodeError):
                lines.append(response.text)
        else:
            try:
                lines.append(response.text)
            except UnicodeDecodeError:
                lines.append(f"<Binary content: {len(response.content)} bytes>")

    return "\n".join(lines)
