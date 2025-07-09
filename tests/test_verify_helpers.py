def verify_response_has_json(response):
    return response.json() is not None


def verify_response_status(response, expected_status=200):
    return response.status_code == expected_status


def verify_response_has_header(response, header_name):
    return header_name in response.headers


def verify_response_content_type(response, content_type):
    return response.headers.get("content-type", "").startswith(content_type)
