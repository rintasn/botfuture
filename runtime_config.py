"""Consistent settings per bot cycle, without blocking dashboard during network IO."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import config as settings

_current = ContextVar("strategy_snapshot", default=None)


class ConfigView:
    def __getattr__(self, name):
        snapshot = _current.get()
        if snapshot is not None and name in snapshot:
            return deepcopy(snapshot[name])
        return getattr(settings, name)


config = ConfigView()


@contextmanager
def strategy_cycle():
    with settings.RUNTIME_LOCK:
        settings.load_runtime_overrides()
        snapshot = {name: deepcopy(getattr(settings, name)) for name in settings.ADMIN_EDITABLE_CONFIG}
    token = _current.set(snapshot)
    try:
        yield
    finally:
        _current.reset(token)
