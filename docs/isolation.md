# Isolation lab

Five concurrency experiments, each run in two real, interactive `psql` sessions (A and B)
against Postgres 18.3. They show what each isolation level does and doesn't protect against,
and why the wallet's transfer services run at **READ COMMITTED with explicit row locks**
(`DESIGN.md` §4).

- **Script:** [`scripts/isolation_lab.sql`](../scripts/isolation_lab.sql). Every step is marked
  `== 1b (A) ==` and names the session it runs in.
- **Reproduce it:** `scripts/run_isolation_lab.sh` runs the whole lab unattended. It opens a
  detached tmux session `isolab` with two `psql` panes, types each step into the session it
  names, waits for each prompt to come back, and writes both sessions to `notes/lab-A.log` and
  `notes/lab-B.log`. The one lock wait (step 3b) is checked: B must still be blocked 2 s after
  the step is sent, and must return once A commits. Needs the compose stack running. You can
  also paste the steps by hand into two `docker compose -p walletledger exec postgres psql -U
  wallet_owner -d walletledger` sessions; the instructions are at the top of the script.
- **Data:** a throwaway schema `isolation_lab` in the dev database, with two tables, `owners` and
  `accounts`, created at setup and dropped at teardown. The real money tables are never
  touched. `accounts.balance` deliberately has no `CHECK (balance >= 0)`, so anomalies show up
  as wrong numbers rather than errors.
- **Run:** 2026-11-19, Postgres 18.3, as `wallet_owner`. All output below is copied from that run.
  The prompt shows `=>` because `wallet_owner` is not a superuser.

## Which case answers what

> **Brief wording to confirm.** The brief's own names for its four cases aren't in the repo yet.
> The first column describes each case by the behaviour it demonstrates; replace it with the
> brief's wording.

| Brief case | Lab case | What the run shows |
|---|---|---|
| Non-repeatable read: READ COMMITTED vs REPEATABLE READ | [1](#case-1-non-repeatable-read) | READ COMMITTED re-reads see B's commit (100000 → 105000); REPEATABLE READ keeps reading 100000 until its own `COMMIT` |
| Lost update, and how to prevent it | [2](#case-2-lost-update-from-read-check-write), [3](#case-3-the-designs-fix-read-committed--select--for-update) | Read-check-write under READ COMMITTED pays out 140000 while deducting 70000. REPEATABLE READ refuses with `40001`. READ COMMITTED plus `FOR UPDATE` makes B wait and then see the new balance (30000) |
| Write skew: REPEATABLE READ vs SERIALIZABLE | [4](#case-4-write-skew-across-two-rows) | REPEATABLE READ lets two withdrawals from different rows break a cross-row rule (total −100000); SERIALIZABLE cancels B with `40001` (total 50000) |
| A join under REPEATABLE READ, and when the snapshot is taken | [5](#case-5-one-snapshot-across-a-join-taken-at-the-first-query) | Both tables of a join come from one snapshot; that snapshot is taken by the first query, not by `BEGIN` |

Every result matched what the script predicted. The few details worth knowing are under
[Differences from the script's predictions](#differences-from-the-scripts-predictions).

The transcripts below interleave A and B in the order the steps ran. Each one starts from
account 1 = 100000 and account 2 = 100000. Status lines such as `BEGIN` are kept only where they
matter.

---

## Case 1: non-repeatable read

One transaction reads the same row twice while another session commits a change in between.

**READ COMMITTED** (steps 1a–1d):

```text
(A) => BEGIN ISOLATION LEVEL READ COMMITTED;
(A) => SELECT balance AS a_first_read FROM isolation_lab.accounts WHERE id = 1;
 a_first_read
--------------
       100000

(B) => UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;
UPDATE 1

(A) => SELECT balance AS a_second_read FROM isolation_lab.accounts WHERE id = 1;
 a_second_read
---------------
        105000
(A) => COMMIT;
```

**REPEATABLE READ** (steps 1e–1g):

```text
(A) => BEGIN ISOLATION LEVEL REPEATABLE READ;
(A) => SELECT balance AS a_first_read FROM isolation_lab.accounts WHERE id = 1;
 a_first_read
--------------
       100000

(B) => UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;
UPDATE 1

(A) => SELECT balance AS a_second_read FROM isolation_lab.accounts WHERE id = 1;
 a_second_read
---------------
        100000
(A) => COMMIT;
(A) => SELECT balance AS after_both_committed FROM isolation_lab.accounts WHERE id = 1;
 after_both_committed
----------------------
               105000
```

**Conclusion.** Under READ COMMITTED each statement gets a fresh snapshot, so two reads in one
transaction can disagree. Under REPEATABLE READ the whole transaction reads one snapshot, and
B's committed change appears only after A's transaction ends.

---

## Case 2: lost update from read-check-write

Two withdrawals of 70000 from a balance of 100000; only one should succeed. Each session reads
the balance, checks it in application code, and writes the computed new value. This is the
pattern `scripts/naive_transfer_demo.py` (step 2.4) runs under load.

**READ COMMITTED** (steps 2a–2d):

```text
(A) => BEGIN ISOLATION LEVEL READ COMMITTED;
(A) => SELECT balance AS a_sees FROM isolation_lab.accounts WHERE id = 1;
 a_sees
--------
 100000

(B) => BEGIN ISOLATION LEVEL READ COMMITTED;
(B) => SELECT balance AS b_sees FROM isolation_lab.accounts WHERE id = 1;
 b_sees
--------
 100000

(A) => UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
UPDATE 1
(A) => COMMIT;
COMMIT

(B) => UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
UPDATE 1
(B) => COMMIT;
COMMIT
(B) => SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
 final_balance
---------------
         30000
```

**REPEATABLE READ** (steps 2e–2h):

```text
(A) => BEGIN ISOLATION LEVEL REPEATABLE READ;
(A) => SELECT balance AS a_sees FROM isolation_lab.accounts WHERE id = 1;
 a_sees
--------
 100000

(B) => BEGIN ISOLATION LEVEL REPEATABLE READ;
(B) => SELECT balance AS b_sees FROM isolation_lab.accounts WHERE id = 1;
 b_sees
--------
 100000

(A) => UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
UPDATE 1
(A) => COMMIT;
COMMIT

(B) => UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
ERROR:  could not serialize access due to concurrent update
(B) => ROLLBACK;
(B) => SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
 final_balance
---------------
         30000
```

**Conclusion.** Under READ COMMITTED both withdrawals commit without any error: 140000 is paid
out and only 70000 is deducted, because B overwrote A's update. REPEATABLE READ detects the
conflicting write to the same row and fails B with `40001`. That's correct, but the client then
has to retry the whole transaction.

---

## Case 3: the design's fix, READ COMMITTED + `SELECT … FOR UPDATE`

The same two withdrawals, but each session locks the row before checking the balance (steps
3a–3d). The run script checked that B's `SELECT … FOR UPDATE` was still blocked 2 seconds after
it was sent, and that it returned once A committed.

```text
(A) => BEGIN ISOLATION LEVEL READ COMMITTED;
(A) => SELECT balance AS a_locked_sees FROM isolation_lab.accounts WHERE id = 1 FOR UPDATE;
 a_locked_sees
---------------
        100000

(B) => BEGIN ISOLATION LEVEL READ COMMITTED;
(B) => SELECT balance AS b_locked_sees FROM isolation_lab.accounts WHERE id = 1 FOR UPDATE;
        -- B blocks here, waiting for A's row lock

(A) => UPDATE isolation_lab.accounts SET balance = balance - 70000 WHERE id = 1;
UPDATE 1
(A) => COMMIT;
COMMIT

        -- B's SELECT returns as soon as A commits:
 b_locked_sees
---------------
         30000
(B) => ROLLBACK;                -- 30000 < 70000: the withdrawal is refused
(B) => SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
 final_balance
---------------
         30000
```

**Conclusion.** B waited for the lock and then read **30000, not 100000**. Under READ COMMITTED,
a `FOR UPDATE` that waited returns the row's latest committed version, so the check made after
taking the lock is always current. The conflicting transactions are serialised, transactions
on other rows run in parallel, and nothing needs a retry loop. This is what every transfer in
the wallet does: it locks all the accounts it touches in ascending id order, then checks
funds.

---

## Case 4: write skew across two rows

The rule is that Asha's **total** across accounts 1 and 2 must stay at or above 0 (it starts at
200000). A withdraws 150000 from account 1 and B withdraws 150000 from account 2. Each checks
the total and then writes a different row.

**REPEATABLE READ** (steps 4a–4d):

```text
(A) => BEGIN ISOLATION LEVEL REPEATABLE READ;
(A) => SELECT sum(balance) AS a_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';
 a_sees_total
--------------
       200000

(B) => BEGIN ISOLATION LEVEL REPEATABLE READ;
(B) => SELECT sum(balance) AS b_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';
 b_sees_total
--------------
       200000

(A) => UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 1;
UPDATE 1
(A) => COMMIT;
COMMIT

(B) => UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 2;
UPDATE 1
(B) => COMMIT;
COMMIT
(B) => SELECT sum(balance) AS final_total FROM isolation_lab.accounts WHERE owner = 'asha';
 final_total
-------------
     -100000
```

**SERIALIZABLE** (steps 4e–4h):

```text
(A) => BEGIN ISOLATION LEVEL SERIALIZABLE;
(A) => SELECT sum(balance) AS a_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';
 a_sees_total
--------------
       200000

(B) => BEGIN ISOLATION LEVEL SERIALIZABLE;
(B) => SELECT sum(balance) AS b_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';
 b_sees_total
--------------
       200000

(A) => UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 1;
UPDATE 1
(A) => COMMIT;
COMMIT

(B) => UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 2;
ERROR:  could not serialize access due to read/write dependencies among transactions
DETAIL:  Reason code: Canceled on identification as a pivot, during write.
HINT:  The transaction might succeed if retried.
(B) => COMMIT;
ROLLBACK
(B) => ROLLBACK;
WARNING:  there is no transaction in progress
(B) => SELECT sum(balance) AS final_total FROM isolation_lab.accounts WHERE owner = 'asha';
 final_total
-------------
       50000
```

**Conclusion.** REPEATABLE READ only catches two writes to the same row. Here the writes hit
different rows, so both commit and the total ends at −100000. SERIALIZABLE tracks what each
transaction read, sees that the two can't be ordered one after the other, and cancels B at its
`UPDATE`. The wallet avoids needing SERIALIZABLE for this by locking every row a rule depends on
before checking it, so no transfer ever checks a row it hasn't locked.

---

## Case 5: one snapshot across a join, taken at the first query

A reads a join of `accounts` and `owners` under REPEATABLE READ, while B commits changes to
both tables (steps 5a–5g). B also commits a change between A's `BEGIN` and A's first query.

```text
(A) => BEGIN ISOLATION LEVEL REPEATABLE READ;
        -- no query yet

(B) => UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;
UPDATE 1                         -- autocommit, after A's BEGIN

(A) => SELECT o.display_name, a.id, a.balance
(A) ->   FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
(A) ->  ORDER BY a.id;
 display_name | id | balance
--------------+----+---------
 Asha Rao     |  1 |  105000
 Asha Rao     |  2 |  100000

(B) => BEGIN;
(B) => UPDATE isolation_lab.owners SET display_name = 'Asha R. Rao' WHERE handle = 'asha';
UPDATE 1
(B) => UPDATE isolation_lab.accounts SET balance = balance + 7000 WHERE id = 1;
UPDATE 1
(B) => COMMIT;
COMMIT

(A) => SELECT o.display_name, a.id, a.balance
(A) ->   FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
(A) ->  ORDER BY a.id;
 display_name | id | balance
--------------+----+---------
 Asha Rao     |  1 |  105000
 Asha Rao     |  2 |  100000

(A) => COMMIT;
(A) => SELECT o.display_name, a.id, a.balance
(A) ->   FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
(A) ->  ORDER BY a.id;
 display_name | id | balance
--------------+----+---------
 Asha R. Rao  |  1 |  112000
 Asha R. Rao  |  2 |  100000
```

**Conclusion.** A's first query saw 105000. B's first change committed after A's `BEGIN` but
before that query, so the snapshot is taken at the first statement, not at `BEGIN`. A's re-run
then showed the old name and the old balance together: neither of B's later changes is visible
in either table, so a join can never mix old and new rows. The reconciliation job relies on
this: it runs all its checks in one REPEATABLE READ transaction (`DESIGN.md` §8.1), so every
check sees the same ledger.

---

## Differences from the script's predictions

Every number and every error matched the script's predictions. Some details are worth recording:

- **Case 4, where B fails.** The script allowed for B to fail either at its `UPDATE` or at
  `COMMIT`. In this run it failed at the `UPDATE` (`Reason code: Canceled on identification as a
  pivot, during write`). The script's `COMMIT` then reported `ROLLBACK`, and its `ROLLBACK`
  printed `WARNING: there is no transaction in progress`, as the script's comment anticipated.
- **The prompt** is `(A) =>`, not `(A) =#`, because `wallet_owner` is not a superuser. Only the
  look changes.
- **The raw logs** (`notes/lab-A.log` and `notes/lab-B.log`) show some lines twice. The runner
  types the next line while psql is still busy, so the terminal echoes it early, and psql shows
  it again when it reads it. The transcripts above remove those early echoes and the runner's
  `== end … ==` sentinels; nothing else is edited.

## What this means for the wallet

- **Transfers** run at READ COMMITTED and lock every account they touch with
  `SELECT … FOR UPDATE`, in ascending id order (case 3). The check after locking always sees the
  current balance, conflicting transfers queue, unrelated ones run in parallel, and clients never
  see serialization failures. Locking in a fixed order means two transfers can't deadlock.
- **REPEATABLE READ or SERIALIZABLE alone** would also block the lost update (cases 2 and 4), but
  by failing a transaction with `40001`, which every client would have to retry. Under
  contention, such as a stampede on one wallet, that means a lot of retries.
- **The plain read-check-write pattern** (case 2 at READ COMMITTED) is exactly the bug step 2.4's
  `naive_transfer_demo.py` reproduces under load.
- **Multi-table reports that must agree with themselves,** such as reconciliation, use one
  REPEATABLE READ snapshot (case 5).
