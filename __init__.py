"""Configurable Workflow Decision Platform (standard library only)."""
import os
import time
from collections import namedtuple

from .config import Registry
from .dependencies import default_dependencies
from .engine import Engine
from .store import Store

App = namedtuple("App", "store registry deps engine")

DEFAULT_CONFIGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "configs")


def build_app(db_path="wfdp.db", configs_dir=DEFAULT_CONFIGS, sleep=time.sleep):
    """Wire store, dependencies, registry and engine together."""
    store = Store(db_path)
    deps = default_dependencies()
    registry = Registry(store, deps.names)
    if configs_dir:
        registry.load_directory(configs_dir)
    return App(store, registry, deps, Engine(store, registry, deps, sleep=sleep))
