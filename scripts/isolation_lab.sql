-- Isolation lab: five concurrency cases, run by hand in two psql sessions, A and B.
--
-- Uses its own throwaway schema `isolation_lab` in the dev database; the real money tables are
-- never touched. Run as wallet_owner (it owns the database, so it can create the schema).
--
-- How to run:
--   1. Open two terminals in the repo and start psql in each:
--        docker compose -p walletledger exec postgres psql -U wallet_owner -d walletledger
--   2. In the first, paste:   \set PROMPT1 '(A) %R%# '     In the second:   \set PROMPT1 '(B) %R%# '
--      so every line of output shows which session it came from. In B also paste:
--      \pset pager off     (SETUP does this for A), so results never stop at a pager.
--   3. Paste SETUP into A once.
--   4. For each case, paste the steps in order, each into the session it names (A or B).
--      Every step starts with an \echo marker such as == 1b (A) ==, so a saved log keeps its order.
--      "B blocks" means B's prompt doesn't come back until A's next step.
--   5. Paste TEARDOWN into A at the end.
--
-- Each case starts with RESET, which puts both rows back to known balances.
-- The rule the "app" enforces in every case: a withdrawal needs balance >= amount.

-- ================================================================================================
-- SETUP (session A, once)
-- ================================================================================================
\pset pager off
\echo '== SETUP (A) =='
DROP SCHEMA IF EXISTS isolation_lab CASCADE;
CREATE SCHEMA isolation_lab;
CREATE TABLE isolation_lab.owners (     -- for case 5's join
    handle       text PRIMARY KEY,
    display_name text NOT NULL
);
INSERT INTO isolation_lab.owners VALUES ('asha', 'Asha Rao');
CREATE TABLE isolation_lab.accounts (
    id      int PRIMARY KEY,
    owner   text   NOT NULL REFERENCES isolation_lab.owners (handle),
    balance bigint NOT NULL            -- paise; deliberately NO CHECK (balance >= 0) here, so the
);                                     -- anomalies show up as wrong numbers instead of errors
INSERT INTO isolation_lab.accounts VALUES (1, 'asha', 100000), (2, 'asha', 100000);
\echo 'setup done: owner asha = Asha Rao; account 1 = 100000, account 2 = 100000'


-- ================================================================================================
-- CASE 1: non-repeatable read. READ COMMITTED vs REPEATABLE READ.
-- One transaction reads the same row twice while another commits a change in between.
-- ================================================================================================

-- 1a (A) RESET
\echo '== 1a (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;

-- 1b (A) start READ COMMITTED, read
\echo '== 1b (A) =='
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance AS a_first_read FROM isolation_lab.accounts WHERE id = 1;

-- 1c (B) credit 5000 and commit
\echo '== 1c (B) =='
UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;

-- 1d (A) read again in the SAME transaction
\echo '== 1d (A) =='
SELECT balance AS a_second_read FROM isolation_lab.accounts WHERE id = 1;
COMMIT;
-- Expected: first 100000, second 105000. Each statement sees the latest committed data.

-- 1e (A) RESET, then the same with REPEATABLE READ
\echo '== 1e (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance AS a_first_read FROM isolation_lab.accounts WHERE id = 1;

-- 1f (B) credit 5000 and commit
\echo '== 1f (B) =='
UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;

-- 1g (A) read again
\echo '== 1g (A) =='
SELECT balance AS a_second_read FROM isolation_lab.accounts WHERE id = 1;
COMMIT;
SELECT balance AS after_both_committed FROM isolation_lab.accounts WHERE id = 1;
-- Expected: both reads 100000 (one snapshot for the whole transaction); 105000 after COMMIT.


-- ================================================================================================
-- CASE 2: lost update via read-check-write. What scripts/naive_transfer_demo.py (2.4) does.
-- Two withdrawals of 70000 from a balance of 100000. Only one should succeed.
-- The "app" reads the balance, checks it in its head, then writes the computed new value.
-- ================================================================================================

-- 2a (A) RESET; start READ COMMITTED; read
\echo '== 2a (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance AS a_sees FROM isolation_lab.accounts WHERE id = 1;

-- 2b (B) start READ COMMITTED; read
\echo '== 2b (B) =='
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance AS b_sees FROM isolation_lab.accounts WHERE id = 1;

-- 2c (A) 100000 >= 70000, so write 100000 - 70000 and commit
\echo '== 2c (A) =='
UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
COMMIT;

-- 2d (B) B also saw 100000, so it writes 30000 too and commits
\echo '== 2d (B) =='
UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
COMMIT;
SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
-- Expected: 30000. Two withdrawals of 70000 (140000 paid out) but only 70000 deducted:
-- A's update was silently overwritten. Nothing errored.

-- 2e (A) RESET; same steps under REPEATABLE READ
\echo '== 2e (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance AS a_sees FROM isolation_lab.accounts WHERE id = 1;

-- 2f (B)
\echo '== 2f (B) =='
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT balance AS b_sees FROM isolation_lab.accounts WHERE id = 1;

-- 2g (A)
\echo '== 2g (A) =='
UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
COMMIT;

-- 2h (B)
\echo '== 2h (B) =='
UPDATE isolation_lab.accounts SET balance = 30000 WHERE id = 1;
-- Expected: ERROR 40001 "could not serialize access due to concurrent update". B must ROLLBACK
-- and retry the whole transaction; on retry it would see 30000 and refuse.
ROLLBACK;
SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
-- Expected: 30000, one withdrawal applied.


-- ================================================================================================
-- CASE 3: the design's fix. READ COMMITTED + SELECT ... FOR UPDATE (DESIGN.md §4).
-- Lock the row, then check. The second withdrawal waits for the lock, then reads the current
-- committed balance and refuses. No retry loop needed.
-- ================================================================================================

-- 3a (A) RESET; lock and read
\echo '== 3a (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance AS a_locked_sees FROM isolation_lab.accounts WHERE id = 1 FOR UPDATE;

-- 3b (B) lock and read: B BLOCKS here
\echo '== 3b (B) =='
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance AS b_locked_sees FROM isolation_lab.accounts WHERE id = 1 FOR UPDATE;

-- 3c (A) 100000 >= 70000: withdraw and commit. B's SELECT returns right after this COMMIT.
\echo '== 3c (A) =='
UPDATE isolation_lab.accounts SET balance = balance - 70000 WHERE id = 1;
COMMIT;

-- 3d (B) B saw 30000 after the wait, 30000 < 70000: refuse
\echo '== 3d (B) =='
ROLLBACK;
SELECT balance AS final_balance FROM isolation_lab.accounts WHERE id = 1;
-- Expected: b_locked_sees = 30000 (not 100000), final 30000. Under READ COMMITTED a FOR UPDATE
-- that waited returns the row's latest committed version, so the post-lock check is current.


-- ================================================================================================
-- CASE 4: write skew across two rows. REPEATABLE READ vs SERIALIZABLE.
-- Rule: Asha's TOTAL across accounts 1 and 2 must stay >= 0. Total is 200000.
-- A withdraws 150000 from account 1, B withdraws 150000 from account 2. Each checks the total,
-- then writes a DIFFERENT row, so neither sees a conflicting update.
-- ================================================================================================

-- 4a (A) RESET; start REPEATABLE READ; check the total
\echo '== 4a (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT sum(balance) AS a_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';

-- 4b (B) same
\echo '== 4b (B) =='
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT sum(balance) AS b_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';

-- 4c (A) 200000 - 150000 >= 0: withdraw from account 1
\echo '== 4c (A) =='
UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 1;
COMMIT;

-- 4d (B) B also saw 200000: withdraw from account 2
\echo '== 4d (B) =='
UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 2;
COMMIT;
SELECT sum(balance) AS final_total FROM isolation_lab.accounts WHERE owner = 'asha';
-- Expected: both commit, total = -100000. REPEATABLE READ only detects conflicting writes to the
-- SAME row; here the rows differ.

-- 4e (A) RESET; same steps under SERIALIZABLE
\echo '== 4e (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT sum(balance) AS a_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';

-- 4f (B)
\echo '== 4f (B) =='
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT sum(balance) AS b_sees_total FROM isolation_lab.accounts WHERE owner = 'asha';

-- 4g (A)
\echo '== 4g (A) =='
UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 1;
COMMIT;

-- 4h (B)
\echo '== 4h (B) =='
UPDATE isolation_lab.accounts SET balance = balance - 150000 WHERE id = 2;
COMMIT;
-- Expected: B fails with ERROR 40001 "could not serialize access due to read/write dependencies
-- among transactions", at the UPDATE or at COMMIT. If the error came at the UPDATE, run ROLLBACK.
ROLLBACK;
SELECT sum(balance) AS final_total FROM isolation_lab.accounts WHERE owner = 'asha';
-- Expected: 50000, only A's withdrawal applied.
-- The wallet design never needs a rule across rows it hasn't locked: each transfer locks every
-- account it checks (ascending id order), so READ COMMITTED + FOR UPDATE covers this too.


-- ================================================================================================
-- CASE 5: one snapshot across a join, taken at the first query (not at BEGIN). REPEATABLE READ.
-- A reads a join of two tables. B changes a row in EACH table and commits. A's re-run of the
-- join still shows the old values from both tables: the whole transaction reads one snapshot.
-- That snapshot is taken by A's first query, not by BEGIN: B's change committed between A's
-- BEGIN and A's first query is visible.
-- ================================================================================================

-- 5a (A) RESET both tables
\echo '== 5a (A) =='
UPDATE isolation_lab.accounts SET balance = 100000;
UPDATE isolation_lab.owners SET display_name = 'Asha Rao' WHERE handle = 'asha';

-- 5b (A) BEGIN only: no query yet, so no snapshot yet
\echo '== 5b (A) =='
BEGIN ISOLATION LEVEL REPEATABLE READ;

-- 5c (B) before A's first query: credit 5000 to account 1 (autocommit)
\echo '== 5c (B) =='
UPDATE isolation_lab.accounts SET balance = balance + 5000 WHERE id = 1;

-- 5d (A) first query: this takes the snapshot
\echo '== 5d (A) =='
SELECT o.display_name, a.id, a.balance
  FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
 ORDER BY a.id;
-- Expected: Asha Rao | 1 | 105000, and Asha Rao | 2 | 100000. B's 5c change IS visible: it
-- committed before the snapshot was taken, even though it committed after A's BEGIN.

-- 5e (B) change a row in each table, in one transaction, and commit
\echo '== 5e (B) =='
BEGIN;
UPDATE isolation_lab.owners SET display_name = 'Asha R. Rao' WHERE handle = 'asha';
UPDATE isolation_lab.accounts SET balance = balance + 7000 WHERE id = 1;
COMMIT;

-- 5f (A) re-run the same join in the same transaction
\echo '== 5f (A) =='
SELECT o.display_name, a.id, a.balance
  FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
 ORDER BY a.id;
-- Expected: unchanged from 5d. Still 'Asha Rao' and 105000: neither of B's committed changes
-- is visible, in either table, so the join never mixes old and new rows.

-- 5g (A) end the transaction; a new statement gets a new snapshot
\echo '== 5g (A) =='
COMMIT;
SELECT o.display_name, a.id, a.balance
  FROM isolation_lab.accounts a JOIN isolation_lab.owners o ON o.handle = a.owner
 ORDER BY a.id;
-- Expected: Asha R. Rao | 1 | 112000, and Asha R. Rao | 2 | 100000.


-- ================================================================================================
-- TEARDOWN (session A)
-- ================================================================================================
\echo '== TEARDOWN (A) =='
DROP SCHEMA isolation_lab CASCADE;
\echo 'teardown done'
