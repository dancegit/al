import importlib


def test_plugin_by_name():
    plugin = importlib.import_module("pkg." + "plugin")
    assert plugin.discount(10) == 9
