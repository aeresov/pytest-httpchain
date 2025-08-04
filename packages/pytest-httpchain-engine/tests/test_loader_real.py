from pytest_httpchain_engine.loader import load_json


def test_user_session(datadir):
    json_file = datadir / "test_user_session.json"
    result = load_json(json_file)
    assert result["stages"][0]["name"] == "Login and acquire session key"
    assert result["stages"][-1]["name"] == "Logout with session key"


def test_dms_154100(datadir):
    json_file = datadir / "test_DMS-154100.json"
    result = load_json(json_file)
    assert result["stages"][0]["name"] == "Login and acquire session key"
    assert result["stages"][-1]["name"] == "Logout with session key"
