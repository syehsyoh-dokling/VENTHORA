# ruff: noqa: E501
"""What VENTHORA does when the server that pays dies, or when something dangerous is happening.

Two rules decide everything here:

  1. Money is never paid twice. A server that takes over knows nothing it can trust about the last minutes of the
     server it replaces, so before it pays anything it reads from Airwallex every transfer that was made and locks
     each one (takeover.catch_up_payments). The bank is the record of what was paid, not our own copy.
  2. Only one server pays. Whoever pays holds the writer lease (lease.py) in a database both servers reach. A server
     that cannot renew its lease stops paying by itself, so a primary that is cut off, but alive, cannot pay next
     to the standby that replaced it.

The parts, each small enough to read in one sitting:

  db.py         one way to reach a store: a SQLite file, or PostgreSQL by URL, with the SQL differences translated
  lease.py      the writer lease: acquire, renew, release, expire
  replicate.py  copy every store to PostgreSQL (a consistent snapshot of each, verified by row counts)
  takeover.py   what a standby does, in order: take the lease, load the data, catch up from Airwallex, re-read the
                last 24 hours of mail, start in safe mode, report
  guard.py      safe mode (no automatic payment until a person lifts it) and the question every payer asks first:
                may this server pay?

What is recovered and what is not, after a takeover:

  payments, conversions, deposits     recovered: read again from Airwallex
  invoices and claims sent by email   recovered: the mailbox is read again for the last 24 hours; a message already
                                      seen is known by its Message-ID and skipped
  approvals, call-backs, settings and
  uploaded bills since the last copy  lost: they exist only on the server that died. The copy runs on a timer from
                                      the host's scheduler, so this is minutes of people's work, never money

Status, stated plainly: this is a design with its code and tests. The lease, the catch-up and safe mode are wired
into the running service and tested with fakes; the database layer and the replication are tested on SQLite, with the
PostgreSQL dialect tested as translation only. No standby server runs anywhere, and no takeover has been rehearsed
against a real PostgreSQL. The stores themselves still read and write SQLite; moving one to PostgreSQL is opening it
through db.connect instead of sqlite3.connect.
"""
