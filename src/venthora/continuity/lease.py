# ruff: noqa: E501
"""The writer lease: only the server that holds it may pay.

One row in a database every server reaches. A server takes the lease when nobody holds it or the holder's time has
run out, and renews it while it works. The time on the row, not a server's own opinion of itself, decides who pays:

  * the primary dies            its lease runs out after `ttl` seconds; the standby takes it
  * the primary is cut off      it cannot renew, so `holds` turns false and it stops paying, although it is alive;
    from the database           the standby takes the lease when the time runs out. The two never pay side by side
  * the standby starts early    `acquire` fails while the primary's lease is live: nothing happens

Taking and renewing are each ONE conditional UPDATE, so two servers asking at the same instant cannot both win.
`ttl` must be longer than the longest single payment (a transfer takes seconds; the default is three minutes), and a
payer checks `holds` again immediately before it moves money.
"""
from __future__ import annotations

import os
import socket
import time

from . import db

SCHEMA = """CREATE TABLE IF NOT EXISTS writer_lease (
    name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_at REAL NOT NULL, acquired_at REAL NOT NULL)"""
KEYS = {"writer_lease": ["name"]}
DEFAULT_TTL_S = 180.0


def node_id() -> str:
    """This server's name: VENTHORA_NODE when set (app-1, app-2), else the host and process."""
    return os.environ.get("VENTHORA_NODE") or f"{socket.gethostname()}:{os.getpid()}"


class WriterLease:
    def __init__(self, url: str, node: str | None = None, *, name: str = "payments", ttl_s: float = DEFAULT_TTL_S, clock=time.time):
        self.url, self.node, self.name, self.ttl_s, self.clock = url, node or node_id(), name, ttl_s, clock
        self._do(SCHEMA, ())

    def _do(self, sql: str, params: tuple) -> list[dict]:
        conn = db.connect(self.url, KEYS)
        try:
            with conn as c:
                return c.execute(sql, params)
        finally:
            conn.close()

    def holder(self) -> dict | None:
        """Who holds the lease now: {holder, expires_at, live}; None when it was never taken."""
        rows = self._do("SELECT holder, expires_at, acquired_at FROM writer_lease WHERE name = ?", (self.name,))
        return (rows[0] | {"live": rows[0]["expires_at"] > self.clock()}) if rows else None

    def acquire(self) -> bool:
        """Take the lease if it is free, expired, or already ours. True when this server now holds it."""
        now = self.clock()
        self._do("INSERT OR IGNORE INTO writer_lease (name, holder, expires_at, acquired_at) VALUES (?, ?, ?, ?)",
                 (self.name, self.node, now + self.ttl_s, now))
        # one statement decides: it changes the row only for the server that may have it
        self._do("UPDATE writer_lease SET holder = ?, expires_at = ?, acquired_at = CASE WHEN holder = ? THEN acquired_at ELSE ? END"
                 " WHERE name = ? AND (holder = ? OR expires_at <= ?)",
                 (self.node, now + self.ttl_s, self.node, now, self.name, self.node, now))
        return self.holds()

    def renew(self) -> bool:
        """Extend our own live lease. False when it is no longer ours (it ran out and another server took it): stop paying."""
        now = self.clock()
        self._do("UPDATE writer_lease SET expires_at = ? WHERE name = ? AND holder = ? AND expires_at > ?",
                 (now + self.ttl_s, self.name, self.node, now))
        return self.holds()

    def keep(self) -> bool:
        """What a server does before it pays: extend its own lease (also one that ran out while it was idle and that
        nobody took), or take a lease that was never taken. It never takes another server's lease, not even an expired
        one: that is a takeover (acquire), which a person starts and which first locks what the bank already paid."""
        now = self.clock()
        self._do("INSERT OR IGNORE INTO writer_lease (name, holder, expires_at, acquired_at) VALUES (?, ?, ?, ?)",
                 (self.name, self.node, now + self.ttl_s, now))
        self._do("UPDATE writer_lease SET expires_at = ? WHERE name = ? AND holder = ?", (now + self.ttl_s, self.name, self.node))
        return self.holds()

    def holds(self) -> bool:
        h = self.holder()
        return bool(h and h["holder"] == self.node and h["live"])

    def release(self) -> None:
        """Give the lease up at once (a planned switch): the other server need not wait for it to run out."""
        self._do("UPDATE writer_lease SET expires_at = ? WHERE name = ? AND holder = ?", (0.0, self.name, self.node))
