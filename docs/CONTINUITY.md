# Continuity: a dead server, a dangerous incident

What VENTHORA does when the server that pays dies, and when something dangerous is happening while it is alive.
The code is in `src/venthora/continuity/` (five small files) and `src/venthora/routers/continuity.py`; the page is
`/continuity`, linked beside "Second factor" in the sidebar.

## Status, stated plainly

| | |
|---|---|
| Safe mode | Works on every deployment, including the staging demo. |
| Writer lease, catch-up from Airwallex, takeover sequence | Written, wired into the running service, tested with fakes (`tests/test_continuity.py`, `tests/test_continuity_api.py`). |
| Copy of the stores to PostgreSQL | Written and tested on SQLite; the PostgreSQL dialect is tested as SQL translation only. |
| A standby server | **None runs anywhere.** No takeover has been rehearsed against a real PostgreSQL. |

The staging demo is one server. Its `/continuity` page shows that, and the two standby actions answer `409` with:
"No standby is connected in this environment. This deployment runs on one server…" and the configuration that
switches failover on. Nothing pretends to work.

## Two rules

1. **Money is never paid twice.** A server that takes over cannot trust what it knows about the last minutes of the
   server it replaces. Before it pays anything it reads every transfer from Airwallex and locks each one in its own
   payout register. The bank is the record of what was paid, not our copy.
2. **Only one server pays.** Whoever pays holds the writer lease, a row in a database both servers reach. A server
   that cannot renew it stops paying by itself, so a primary that is cut off but alive cannot pay next to the
   standby that replaced it.

## The layout (option B)

```
            app-1 (primary)                         app-2 (standby)
            SQLite stores  ──── copy ────►  PostgreSQL  ◄──── reads and writes directly
            holds the lease ─────────────►  writer_lease row  ◄──── watches the lease
                    │                                                  │
                    └──────────────►  Airwallex  ◄─────────────────────┘
                                  (the record of every payment)
```

- **Two locations.** In production the SQLite stores (with the primary) and PostgreSQL (with the standby) are on
  servers in two different locations, so that no single incident (a host, a disk, a data centre, a network) takes
  both the data and its copy.
- **Two things switch on at different times.** The copy to PostgreSQL is the backup, and it starts on day one of a
  production deployment, before any standby exists: from then on the data is already in the second location. The
  takeover on the other server is switched on only when that second server is there. On this one-server demo no
  PostgreSQL is connected, so neither runs here.
- The primary copies every store to PostgreSQL on a timer (`python -m venthora.continuity.replicate` from the host's
  scheduler, every few minutes) and on request (`POST /v1/continuity/replicate`): a consistent snapshot of each
  store, one transaction per store, row counts checked. The service does not start the copy by itself yet.
- The standby does not keep a second SQLite copy. It opens the stores in PostgreSQL through `db.connect`, the same
  code path with the SQL differences translated.
- The lease lasts 180 seconds and is renewed by every payment. Taking it is one SQL statement that changes the row
  only for the server that may have it, so two servers asking at the same moment cannot both win.

## A dead server: the takeover, in order

`takeover.take_over`, run by `POST /v1/continuity/takeover` (FCO only):

1. **Take the lease.** Refused while the other server's lease is live: it is alive, nothing is changed.
2. **Safe mode on.** Nothing pays by itself until a person lifts it.
3. **Catch up from Airwallex.** Every transfer the bank knows is locked by its reference (VENTHORA writes the request
   and the invoice into every transfer's reference), so the plan cannot pay it again. A failed or cancelled transfer
   is not locked.
4. **Read the mail again** for the last 24 hours. A message already seen is known by its Message-ID and skipped.
5. **Report** what was locked, what was read, how old the copied data is, and what a person should look at before
   lifting safe mode.

### What is recovered, and what is not

| | |
|---|---|
| Payments, conversions, deposits | Recovered: read again from Airwallex. |
| Invoices and claims sent by email | Recovered: the mailbox is read again for 24 hours. |
| Approvals, call-backs, settings, uploaded bills since the last copy | **Lost.** They exist only on the server that died. This is minutes of people's work, never money: an approval that is lost is asked for again. |

### The primary comes back

It no longer holds the lease. The executor asks `guard.may_pay` immediately before money moves
(`executor.writer_gate`), and the answer is no: the run ends `REFUSED`, "this server (app-1) is not the writer: app-2
holds the lease", before anything is planned or moved. If the lease database cannot be read at all, the answer is
also no.

## A dangerous incident: safe mode

`PUT /v1/continuity/safe-mode` with a reason (FCO only), or the button on `/continuity`. It stops the three things
that pay without a person: the scheduled run, payment on arrival, and the autopilot run. A person can still approve
and pay one request. Turning it on and lifting it are written to the audit trail with the name and the reason.

Use it when a payment went somewhere it should not have, when credentials may be exposed, or when the bank's data
looks wrong. The other incident steps (freezing cards, revoking keys, restore) are in
[OPERATIONS.md](OPERATIONS.md).

## Configuration

Failover is switched on by configuration, on both servers. Without it, the service behaves as one server.

| Variable | Meaning |
|---|---|
| `VENTHORA_CONTINUITY_URL` | The shared PostgreSQL that holds the writer lease. Setting it makes every payment ask for the lease. |
| `VENTHORA_REPLICA_URL` | Where the stores are copied: PostgreSQL in the second location. Set this first, on its own, to start the backup. |
| `VENTHORA_NODE` | This server's name in the lease and the reports (`app-1`, `app-2`). |

## What a production deployment still needs

- First, a managed PostgreSQL in a second location and the copy running to it from day one (`VENTHORA_REPLICA_URL`
  alone is enough for that).
- Then a second server in that location, and one rehearsed takeover against it.
- The stores opened through `db.connect` on the standby (the layer exists; the stores still call SQLite directly).
- The mail reader connected to the takeover (`app.state.reread_mail`); until then the report says to run Email Sync
  by hand.
- A health check outside both servers that tells the FCO the primary is gone. The takeover itself stays a person's
  decision: a wrong automatic takeover is worse than a few minutes without payments.
