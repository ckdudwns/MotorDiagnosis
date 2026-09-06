"""Coordinate device identity changes with independent history stores.

The application owns one in-process master state. Acquire this gate before
STORE_LOCK or an independent store lock; never wait on a history DB while
holding STORE_LOCK. Multiple connections share the gate, not just one server.
"""

from functools import wraps
from threading import RLock
from weakref import WeakSet

_gate = RLock()
_history_stores = WeakSet()


def serialized(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with _gate:
            return function(*args, **kwargs)

    return guarded


@serialized
def register_history(store):
    _history_stores.add(store)


@serialized
def unregister_history(store):
    _history_stores.discard(store)


@serialized
def check_history(device_id):
    """Fail closed if any active history store rejects removal or ID reuse."""
    for store in tuple(_history_stores):
        store.check_device_deletion(device_id)
