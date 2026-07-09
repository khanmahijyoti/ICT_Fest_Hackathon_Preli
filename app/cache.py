"""In-memory response caches for read-heavy reporting endpoints.

Usage reports and per-room availability are relatively expensive to compute and
are read far more often than the underlying data changes, so results are cached
and invalidated when the data they depend on is modified.
"""
import threading

_report_cache: dict[tuple, dict] = {}
_availability_cache: dict[tuple, dict] = {}
_report_lock = threading.Lock()
_availability_lock = threading.Lock()


def get_report(org_id: int, frm: str, to: str):
    with _report_lock:
        return _report_cache.get((org_id, frm, to))


def set_report(org_id: int, frm: str, to: str, value: dict) -> None:
    with _report_lock:
        _report_cache[(org_id, frm, to)] = value


def invalidate_report(org_id: int) -> None:
    with _report_lock:
        for key in [k for k in _report_cache if k[0] == org_id]:
            _report_cache.pop(key, None)


def get_availability(room_id: int, date: str):
    with _availability_lock:
        return _availability_cache.get((room_id, date))


def set_availability(room_id: int, date: str, value: dict) -> None:
    with _availability_lock:
        _availability_cache[(room_id, date)] = value


def invalidate_availability(room_id: int, date: str) -> None:
    with _availability_lock:
        _availability_cache.pop((room_id, date), None)
