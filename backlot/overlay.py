"""The mutation overlay: a writable in-memory database attached to the read-only corpus.

Served writes land here. The corpus connection is opened ``mode=ro``, which SQLite applies to
``main`` alone, so an ATTACHed database on the same connection takes writes while the corpus file
stays untouchable — the immutability is enforced rather than agreed to.

Its tables are GENERATED, one set per source in :data:`store.WRITABLE`, for the reason
``store.SCHEMA`` generates the eleven ``<source>_acl`` tables instead of writing eleven blocks:
hand-written per-source DDL is one place to drift apart per source. The document mirror goes
further and is read out of the corpus itself, so it cannot diverge from the table it mirrors.

This module knows SQLite and the registries. What the rows MEAN is :mod:`backlot.store`'s.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading

from backlot import store

# One lock for every overlay write, over the single shared `app.state.conn`.
#
# Not for the Slack handlers against each other: all of them are `async def`, so they run on the
# event loop one at a time. It is the two `/_meta/overlay*` handlers that need it -- those are
# plain `def`, so Starlette runs them on the threadpool, and a `reset` there can land between two
# statements of a write already in flight.
#
# Module-level rather than per-connection because `sqlite3.Connection` has no `__dict__` and
# cannot be weak-referenced, so there is nowhere to hang per-connection state.
LOCK = threading.Lock()

# Names are handed out from a counter, never derived from an object's identity. `id(app)` would
# repeat: CPython reuses the address of a garbage-collected object, so a later app could land on a
# freed one's address and attach to the overlay that app left behind.
_COUNTER = itertools.count()


def table_names(source_type: str) -> dict[str, str]:
    """The five generated overlay tables for one source, by role.

    ``doc`` and ``acl`` keep the corpus's own names so a merged query reads the same identifier on
    both sides; the other three are new concepts and are named after the source.
    """
    return {
        "doc": store.table(source_type),
        "acl": store.acl_table(source_type),
        "patch": f"{source_type}_patch",
        "tombstone": f"{source_type}_tombstone",
        "fts": f"{source_type}_fts_ov",
    }


def _uri(name: str) -> str:
    # A shared-cache in-memory database is keyed on its NAME. A constant would make every server
    # in one process share one overlay, which in a pytest run is every module-scoped client.
    return f"file:{name}?mode=memory&cache=shared"


def name_for(app) -> str:
    """A name unique to one app instance, so two servers in a process get two overlays.

    ``app`` is taken but not read: the name has to be unique per call, and an identity-derived one
    is not (see :data:`_COUNTER`). The parameter stays because the caller has an app and passing
    it reads as what this is for.
    """
    return f"backlot_ov_{next(_COUNTER)}"


def is_attached(conn: sqlite3.Connection) -> bool:
    """Whether this connection has the overlay.

    Asked of the connection rather than cached on it: ``sqlite3.Connection`` has no ``__dict__``
    and cannot be weak-referenced, so there is nowhere to put a flag. ``store._has_fts`` answers
    its own question the same way, and for the same reason this is cheap — ``database_list`` is a
    pragma over already-open handles, not a disk read.
    """
    return any(row[1] == "ov" for row in conn.execute("PRAGMA database_list"))


def _mirror_ddl(conn: sqlite3.Connection, table: str, *, extra: str = "") -> str:
    """DDL for an overlay table with the corpus table's columns and primary key.

    Read out of ``main`` rather than restated in this repository, so a column a later import adds
    to the corpus appears in the overlay without anyone remembering to add it. ``table_info``
    reports ``(cid, name, type, notnull, dflt_value, pk)``, with ``pk`` carrying the column's
    1-based position in the key.

    The DEFAULT is carried, not just the type and the NOT NULL. Dropping it makes a column the
    corpus declares ``NOT NULL DEFAULT 0`` reject an insert that omits it, so a mirror missing
    defaults is not a mirror — every writer would have to restate values the corpus supplies.
    ``dflt_value`` comes back already quoted the way the schema wrote it, so it is spliced
    verbatim.
    """
    cols = conn.execute(f"PRAGMA main.table_info({table})").fetchall()
    if not cols:
        raise ValueError(f"overlay: corpus has no table {table!r} to mirror")
    decl = ", ".join(
        f"{c[1]} {c[2]}"
        + (" NOT NULL" if c[3] else "")
        + (f" DEFAULT {c[4]}" if c[4] is not None else "")
        for c in cols
    )
    key = [c[1] for c in sorted((c for c in cols if c[5]), key=lambda c: c[5])]
    pk = f", PRIMARY KEY ({', '.join(key)})" if key else ""
    return f"CREATE TABLE IF NOT EXISTS ov.{table} ({decl}{extra}{pk})"


def _source_ddl(conn: sqlite3.Connection, source_type: str) -> list[str]:
    """Every statement that creates one source's generated overlay tables."""
    names = table_names(source_type)
    key = store.id_columns(source_type)
    key_decl = ", ".join(f"{c} {store.id_column_type(source_type, c)} NOT NULL" for c in key)
    key_cols = ", ".join(key)
    fts_decl = ", ".join(f"{c} UNINDEXED" for c in key)
    fts_text = ", ".join(store._fts_text_columns(source_type))
    return [
        # The document mirror, and the ACL rows a written document needs: a grant names its
        # document by ID_COLUMNS, so a row written without grants is visible to nobody at all,
        # including its own author. Nothing records a revocation: a corpus grant cannot be
        # deleted, so revoking one will need a column here to mark it with.
        _mirror_ddl(conn, names["doc"]),
        _mirror_ddl(conn, names["acl"]),
        # An overwrite of ONE column of a row that may live in either database. A corpus row is a
        # dozen columns and copying it to change one only moves the question of which copy is
        # authoritative; this says exactly what changed.
        f"CREATE TABLE IF NOT EXISTS ov.{names['patch']} "
        f"({key_decl}, field TEXT NOT NULL, value TEXT, PRIMARY KEY ({key_cols}, field))",
        # A corpus row cannot be removed, so a delete is subtracted at read time.
        f"CREATE TABLE IF NOT EXISTS ov.{names['tombstone']} "
        f"({key_decl}, PRIMARY KEY ({key_cols}))",
        # Declared exactly like the corpus index (`store.build_fts`) so the same query syntax
        # matches the same way. Its own bm25 is NOT used: a term in a near-empty index has no
        # inverse document frequency, so an overlay hit would sort last however well it matches
        # -- measured, identical text scores -2.70 in a 5,000-document index and -1e-06 in a
        # one-document one. `store` scores these rows against the corpus's statistics instead.
        # The index is here for MATCHING -- phrases, prefixes, NEAR, the porter tokenizer -- none
        # of which is worth reimplementing in Python.
        f"CREATE VIRTUAL TABLE IF NOT EXISTS ov.{names['fts']} "
        f"USING fts5({fts_decl}, {fts_text}, tokenize='porter unicode61')",
    ]


def _acl_view_ddl(conn: sqlite3.Connection, source_type: str) -> str:
    """A ``temp`` view that shadows the source's ACL table with corpus-plus-overlay grants.

    Seventy-four call sites reach the ACL through :func:`store._acl_clause`, which emits an
    unqualified table name — so an overlay grant would be invisible to every one of them, and a
    posted message would be readable by nobody including its author. Threading a connection
    through all of them to union the two tables would be the alternative.

    A ``temp`` view is the whole fix instead, because SQLite resolves an unqualified name in
    ``temp`` before ``main`` (measured). Every reader keeps its query verbatim and sees one ACL
    relation; ``main.<acl>`` stays reachable, qualified, for the writer that copies grants.

    Created by the first write, not by `attach`: a `UNION ALL` is not free even over an empty
    table, and a server nothing has been written to should read the ACL table itself. With a
    person's token, `conversations.history` over a 200,000-message channel took 94 ms behind the
    view against 72 ms on `main.<acl>`.
    """
    names = table_names(source_type)
    acl = names["acl"]
    cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({acl})")]
    projection = ", ".join(cols)
    return (
        f"CREATE VIEW IF NOT EXISTS temp.{acl} AS "
        f"SELECT {projection} FROM main.{acl} "
        f"UNION ALL "
        f"SELECT {projection} FROM ov.{acl}"
    )


def attach(conn: sqlite3.Connection, name: str) -> None:
    """Attach an empty overlay as ``ov`` and create every writable source's tables in it."""
    conn.execute("ATTACH DATABASE ? AS ov", (_uri(name),))
    for source_type in sorted(store.WRITABLE):
        for ddl in _source_ddl(conn, source_type):
            conn.execute(ddl)
    conn.commit()


def ensure_acl_view(conn: sqlite3.Connection, source_type: str) -> None:
    """Put the ACL view in front of the corpus's table, if it is not there already.

    Called by every write rather than by :func:`attach`, so a server nothing has been written to
    reads `main.<acl>` with its own index (:func:`_acl_view_ddl`). `CREATE VIEW IF NOT EXISTS`
    makes the repeat calls free.
    """
    conn.execute(_acl_view_ddl(conn, source_type))


def detach(conn: sqlite3.Connection) -> None:
    """Drop the overlay, and the temp views that read from it first.

    A view left behind would name a schema that no longer exists, so the next unqualified ACL read
    fails rather than falling back to the corpus.
    """
    for source_type in sorted(store.WRITABLE):
        conn.execute(f"DROP VIEW IF EXISTS temp.{table_names(source_type)['acl']}")
    conn.execute("DETACH DATABASE ov")


def reset(conn: sqlite3.Connection, name: str) -> None:
    """Throw the overlay away and start a new one. An evaluation run's teardown.

    Rolls back first: SQLite refuses to DETACH a database inside an open transaction (``database
    ov is locked``), and a caller that wrote without committing would otherwise make reset fail
    rather than do the one thing it promises. Discarding the pending write is exactly what reset
    means, and ``main`` is read-only, so there is nothing else in the transaction to lose.
    """
    with LOCK:
        conn.rollback()
        detach(conn)
        attach(conn, name)
