"""Run slow jobs (analysis, backtests) in a background thread so web requests return at once.

Used on hosting without a separate worker. One job per key at a time in this process.
"""

import logging
import threading

from django.db import close_old_connections, connection

log = logging.getLogger("apps.core")
_running = set()
_lock = threading.Lock()


def is_running(key):
    return key in _running


def start(key, fn, *args, **kwargs):
    """Start fn in a thread unless a job with this key is already running. Returns True if started."""
    with _lock:
        if key in _running:
            return False
        _running.add(key)

    def job():
        close_old_connections()
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Background job %s failed", key)
        finally:
            connection.close()
            with _lock:
                _running.discard(key)

    threading.Thread(target=job, name=f"job:{key}", daemon=True).start()
    return True
