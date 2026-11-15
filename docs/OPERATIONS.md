# Operations: incidents, backup and restore

How VENTHORA is kept running and brought back. What has been rehearsed is said so; what has not is said so too.

## Where the data is

Every store is a SQLite file under the data directory (one set per company under `data/saas/<company>/` on a SaaS
deployment):

| Store | File | What is lost without it |
|---|---|---|
| Audit chain | `venthora_audit.db` | The record of every decision, approval and execution |
| Execution | `venthora_execution.db` | Payout claims, request ids, the book ledger, the spend ledger |
| Intake | `venthora_intake.db` | Payment requests and their checks |
| Organisation | `venthora_org.db` | Offices, people, budgets, purchase orders, settings |
| Master data | `venthora_master.db` | Vendors, customers, bills |
| Secrets | `venthora_secrets.db` | Password hashes, keys, sessions |
| Webhooks, AI | `venthora_webhooks.db`, `venthora_ai.db` | Event history, model settings and usage |

Money itself is at Airwallex. Losing a store never loses money; it loses VENTHORA's record of it and the locks that
stop a payment from being made twice. That is why the execution store and the audit chain are restored first.

## Backup

```bash
python scripts/backup_stores.py backup  /opt/venthora/data /var/backups/venthora
python scripts/backup_stores.py verify  /var/backups/venthora/<timestamp>
```

- Uses SQLite's online backup: a store being written is copied whole, never half-way through a transaction. The
  service keeps running.
- Each run has its own folder and a manifest: file, size, SHA-256, rows per table.
- `verify` re-checks the checksum, SQLite's integrity check and the row counts.
- Suggested schedule: every hour to local disk, every day to another machine or bucket, kept for 35 days. Copy the
  folder off the host; a backup on the same disk does not survive the disk.

## Restore

```bash
systemctl stop venthora-platform venthora-gateway
python scripts/backup_stores.py restore /var/backups/venthora/<timestamp> /opt/venthora/data
systemctl start venthora-gateway venthora-platform
```

- A backup that does not pass `verify` is refused.
- What is replaced is kept beside it as `<name>.before-restore`; nothing is deleted.
- After a restore, before any payment run:
  1. `GET /api/audit` : the chain must report `verified`.
  2. Agent plan → Today's plan: compare the book balance with the Airwallex wallet.
  3. Books & ledger: any transfer made after the backup is at Airwallex but not in the restored stores. Read
     Airwallex's transfers for the gap and record them (the duplicate lock is by invoice, so recording them first is
     what stops a second payment). **Do not run the payment run until this is done.**

## Incidents

| What happened | First action | Then |
|---|---|---|
| A payment went to the wrong account | Users & Beneficiaries → Payee lifecycle → Suspend the payee (stops every further payment to it) | Contact Airwallex support with the transfer id; the audit chain has who verified the payee and when |
| A payment run misbehaves | Agent plan → Hold on each payment, or set the policy mode off in Standard Payment Priority Policy | Read the operator log (`journalctl -u venthora-platform`), then the audit chain for the run |
| A key or password may be exposed | Platform Setup: replace the Airwallex API key; ask every person to set a new password through a new sign-in link | Review the audit chain for sign-ins and writes since the exposure |
| The server is down | `systemctl status venthora-platform venthora-gateway`; restart | If the data is damaged: Restore, above |
| A webhook flood or a scripted client | The rate limit answers 429 by itself (`hardening.py`); the address is in the operator log | Block the address at the proxy |

Every incident ends with a note of what happened, what was done and what changes; the audit chain is the evidence.

## What has been rehearsed, and what has not

- **Rehearsed by an automated test** (`tests/test_backup.py`): a backup taken, the live store then changed and lost,
  the backup restored, the audit chain intact; a damaged backup refused.
- **Not rehearsed on the live server**: the backup schedule is not installed there, and a restore has never been run
  on it.
- **Not in place**: off-host log shipping and alerting, an on-call rota, a second region.
