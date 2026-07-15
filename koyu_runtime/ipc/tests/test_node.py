from koyu_runtime.ipc import node


def test_node_is_a_singleton():
    assert node.node() is node.node()


def test_name_builds_a_service_name():
    assert node.name("camera/rgb") is not None


def test_sweep_dead_does_not_raise():
    node.sweep_dead()
