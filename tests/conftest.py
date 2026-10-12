import pytest


@pytest.fixture(scope="session")
def _test_appdata(tmp_path_factory):
    return tmp_path_factory.mktemp("appdata")


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, _test_appdata):
    """測試不要寫到使用者真正的 %APPDATA%\\fftoolbox（config.json、字型快取）。"""
    monkeypatch.setenv("APPDATA", str(_test_appdata))
