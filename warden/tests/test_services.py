import pytest

from warden.services import ServicesError, load_services, parse_services


def spec_of(stanza, name="svc"):
    return parse_services({name: stanza})[name]


def test_minimal_defaults():
    s = spec_of({"cmd": ["python3", "main.py"]})
    assert s.name == "svc"
    assert s.cmd == ["python3", "main.py"]
    assert s.env == {}
    assert s.autostart is True
    assert s.autorestart == "unexpected"
    assert s.startsecs == 1
    assert s.startretries == 3
    assert s.stopwaitsecs == 10
    assert s.exitcodes == [0]
    assert s.oneshot is False


def test_missing_cmd_raises():
    with pytest.raises(ServicesError, match="missing required field 'cmd'"):
        spec_of({"env": {"A": "1"}})


def test_empty_cmd_raises():
    with pytest.raises(ServicesError, match="non-empty list"):
        spec_of({"cmd": []})


def test_cmd_not_list_raises():
    with pytest.raises(ServicesError, match="non-empty list"):
        spec_of({"cmd": "python3 main.py"})


def test_env_coercion():
    s = spec_of({"cmd": ["x"], "env": {"RATE": 50, "FLAG": True, "NAME": "foo"}})
    assert s.env == {"RATE": "50", "FLAG": "true", "NAME": "foo"}


def test_env_not_map_raises():
    with pytest.raises(ServicesError, match="'env' must be a mapping"):
        spec_of({"cmd": ["x"], "env": ["A=1"]})


def test_reserved_env_key_raises():
    with pytest.raises(ServicesError, match="reserved"):
        spec_of({"cmd": ["x"], "env": {"SERVICE_NAME": "nope"}})


@pytest.mark.parametrize(
    "val,expected",
    [("unexpected", "unexpected"), (True, "true"), (False, "false"), ("true", "true"), ("false", "false")],
)
def test_autorestart_normalization(val, expected):
    assert spec_of({"cmd": ["x"], "autorestart": val}).autorestart == expected


def test_autorestart_invalid_raises():
    with pytest.raises(ServicesError, match="autorestart"):
        spec_of({"cmd": ["x"], "autorestart": "sometimes"})


def test_startsecs_negative_raises():
    with pytest.raises(ServicesError, match="startsecs"):
        spec_of({"cmd": ["x"], "startsecs": -1})


def test_startsecs_non_int_raises():
    with pytest.raises(ServicesError, match="startsecs"):
        spec_of({"cmd": ["x"], "startsecs": "soon"})


def test_startsecs_bool_rejected():
    # bool is an int subclass — make sure we don't accept True as 1.
    with pytest.raises(ServicesError, match="startsecs"):
        spec_of({"cmd": ["x"], "startsecs": True})


def test_exitcodes_int_becomes_list():
    assert spec_of({"cmd": ["x"], "exitcodes": 0}).exitcodes == [0]


def test_exitcodes_list():
    assert spec_of({"cmd": ["x"], "exitcodes": [0, 2]}).exitcodes == [0, 2]


def test_exitcodes_invalid_raises():
    with pytest.raises(ServicesError, match="exitcodes"):
        spec_of({"cmd": ["x"], "exitcodes": ["zero"]})


def test_oneshot_bool():
    assert spec_of({"cmd": ["x"], "oneshot": True}).oneshot is True


def test_oneshot_non_bool_raises():
    with pytest.raises(ServicesError, match="oneshot"):
        spec_of({"cmd": ["x"], "oneshot": "yes please"})


def test_ipc_block_ignored():
    s = spec_of({"cmd": ["x"], "ipc": {"publishes": {"t": "T"}}})
    assert not hasattr(s, "ipc")
    assert s.cmd == ["x"]


def test_empty_file_is_empty():
    assert parse_services(None) == {}


def test_top_level_not_mapping_raises():
    with pytest.raises(ServicesError, match="must be a mapping"):
        parse_services(["a", "b"])


def test_stanza_not_mapping_raises():
    with pytest.raises(ServicesError, match="must be a mapping"):
        parse_services({"svc": "python3 main.py"})


def test_preserves_order():
    assert list(parse_services({"a": {"cmd": ["x"]}, "b": {"cmd": ["y"]}})) == ["a", "b"]


def test_load_services_missing_file(tmp_path):
    with pytest.raises(ServicesError, match="not found"):
        load_services(tmp_path / "nope.yaml")


def test_load_services_roundtrip(tmp_path):
    p = tmp_path / "services.yaml"
    p.write_text("sim:\n  cmd: [python3, sim.py]\n  env:\n    RATE: '50'\n")
    specs = load_services(p)
    assert specs["sim"].cmd == ["python3", "sim.py"]
    assert specs["sim"].env == {"RATE": "50"}
