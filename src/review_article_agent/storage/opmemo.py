"""Operation-scoped validation memo for the storage layer.

Drafting and synthesis re-validate the whole immutable upstream chain on every
read.  Each individual ``require_*`` gate check is deterministic for a given
snapshot of the database, so within a single operation it can be answered once
and reused.  This module provides that reuse without ever letting a result
outlive the operation it was computed in.

Safety argument
---------------
A memo entry may only be reused while it still describes the same database
state the caller would re-derive.  Three properties make that true, and each is
enforced here:

* **Transaction snapshot isolation.**  Caching happens *only* inside a SQLite
  transaction (``conn.in_transaction``).  Outside a transaction the connection
  is in autocommit mode and a concurrent writer can commit between two
  statements, so :func:`memoize` never caches there -- it simply calls
  ``compute``.  Inside a transaction the reads share one coherent snapshot.
* **Own writes are tracked.**  The cache key is ``(key, conn.total_changes)``.
  ``total_changes`` counts the rows this connection has inserted, updated or
  deleted since it was opened; any write the operation makes changes it and so
  misses every earlier entry.  A check that depends on a row the operation
  itself rewrote is therefore recomputed.
* **Savepoint rollback invalidates.**  ``ROLLBACK TO <savepoint>`` reverts
  writes but does **not** decrease ``total_changes``, so an entry cached before
  the rollback could otherwise still be hit while the reverted state is
  current.  Every savepoint rollback site calls :func:`invalidate` immediately
  after the rollback.  The outermost transaction also invalidates on entry and
  in a ``finally`` on exit (commit *or* rollback), so no entry can outlive the
  operation.

There is deliberately **no process-global cache of results**: entries are keyed
per connection and dropped at every transaction boundary, so a stale approval
can never be served to a later, unrelated operation.  Fail-closed behaviour is
unchanged -- a cache miss simply runs the original ``compute`` and lets any
exception propagate unchanged.

``sqlite3.Connection`` is not weak-referenceable (verified on CPython 3.14), so
the per-connection state is keyed by ``id(conn)``.  Entries are always cleared
at the end of the outermost transaction, so a recycled ``id`` can never observe
a stale entry.
"""

from __future__ import annotations

import copy
import sqlite3
import threading

__all__ = ["memoize", "invalidate", "stats"]

# Sentinel distinguishing "no entry" from a cached ``None``.
_MISSING = object()

# A single lock guards the per-connection state map.  ``compute`` is always
# invoked outside the lock, so a slow (or re-entrant) computation never blocks
# other connections.
_LOCK = threading.Lock()
_STATES: "dict[int, _ConnState]" = {}


class _ConnState:
    """Per-connection memo cache plus hit/miss counters for observability."""

    __slots__ = ("cache", "hits", "misses")

    def __init__(self) -> None:
        self.cache: dict[tuple, object] = {}
        self.hits = 0
        self.misses = 0


def _state(cid: int) -> _ConnState:
    state = _STATES.get(cid)
    if state is None:
        state = _ConnState()
        _STATES[cid] = state
    return state


def memoize(conn: sqlite3.Connection, key, compute):
    """Return ``compute()``, reusing an in-transaction result when it is safe.

    ``key`` must be hashable (callers pass tuples of strings); an unhashable
    key raises :class:`TypeError`.  Outside a transaction ``compute`` always
    runs.  Inside one, a hit returns a deep copy of the cached value; a miss
    runs ``compute`` and caches a deep copy of its result.  If ``compute``
    raises, nothing is cached and the exception propagates unchanged.
    """
    try:
        hash(key)
    except TypeError:
        raise TypeError(
            "operation-scoped memo keys must be hashable (e.g. a tuple of "
            "strings)."
        ) from None

    if not conn.in_transaction:
        # Autocommit: another process may commit between statements, so a
        # result computed here is not safe to reuse.
        return compute()

    cid = id(conn)
    entry_key = (key, conn.total_changes)
    with _LOCK:
        state = _STATES.get(cid)
        if state is not None:
            cached = state.cache.get(entry_key, _MISSING)
            if cached is not _MISSING:
                state.hits += 1
                return copy.deepcopy(cached)
        else:
            state = _state(cid)
        state.misses += 1

    result = compute()

    with _LOCK:
        # Stored as a deep copy so the caller mutating the returned value cannot
        # poison later hits; the original result is returned to the caller.
        _state(cid).cache[entry_key] = copy.deepcopy(result)
    return result


def invalidate(conn: sqlite3.Connection) -> None:
    """Drop every memo entry for ``conn`` and reset its hit/miss counters.

    Called at the start and end of every outermost transaction and right after
    every savepoint rollback, so no entry outlives the operation it was
    computed in.
    """
    cid = id(conn)
    with _LOCK:
        state = _STATES.get(cid)
        if state is not None:
            state.cache.clear()
            state.hits = 0
            state.misses = 0


def stats(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return in-transaction ``(hits, misses)`` for ``conn``.

    Counts only :func:`memoize` calls made inside a transaction (autocommit
    calls are never cached, so they are not counted) since the last
    :func:`invalidate`.  Lightweight test observability: later tests can assert
    that a repeated validation was served from the memo without relying on
    timing.
    """
    with _LOCK:
        state = _STATES.get(id(conn))
        if state is None:
            return (0, 0)
        return (state.hits, state.misses)
