import pytest

from warden.supervisord_client import SupervisordClient, SupervisordError


def test_is_running_false_when_no_socket(tmp_path):
    assert SupervisordClient(tmp_path / "nope.sock").is_running() is False


def test_call_raises_control_error_on_dead_socket(tmp_path):
    with pytest.raises(SupervisordError):
        SupervisordClient(tmp_path / "nope.sock").process_info()
