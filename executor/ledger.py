"""
The intent ledger — one row per money-moving intent, guarded transitions.

intent_key is the primary key: two rows with the same intent cannot exist.
State only advances via advance(), a conditional UPDATE that moves a row from an
*expected* state to the next one. It returns True only if exactly one row made
the move — so a blind double-fire, or two executors racing the same intent, can
never both win. This is what actually gates the UI submit click in the executor.
"""
import sqlite3
import json

STATES = ("created", "prepared", "triggered", "confirmed", "aborted", "unknown")


class Ledger:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS intent_ledger (
            intent_key   TEXT PRIMARY KEY,
            task         TEXT NOT NULL,
            task_version TEXT NOT NULL,
            member_ref   TEXT NOT NULL,
            state        TEXT NOT NULL CHECK (state IN
                          ('created','prepared','triggered','confirmed','aborted','unknown')),
            receipt      TEXT,
            updated_at   TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.db.commit()

    def create(self, intent_key, task, version, member_ref):
        self.db.execute(
            "INSERT INTO intent_ledger(intent_key,task,task_version,member_ref,state) "
            "VALUES (?,?,?,?, 'created')", (intent_key, task, version, member_ref))
        self.db.commit()

    def advance(self, intent_key, from_state, to_state):
        """Guarded transition. True iff exactly one row moved from from_state."""
        cur = self.db.execute(
            "UPDATE intent_ledger SET state=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE intent_key=? AND state=?", (to_state, intent_key, from_state))
        self.db.commit()
        return cur.rowcount == 1

    def write_receipt(self, intent_key, receipt):
        self.db.execute("UPDATE intent_ledger SET receipt=? WHERE intent_key=?",
                        (json.dumps(receipt), intent_key))
        self.db.commit()

    def state(self, intent_key):
        row = self.db.execute("SELECT state FROM intent_ledger WHERE intent_key=?",
                              (intent_key,)).fetchone()
        return row[0] if row else None

    def row(self, intent_key):
        r = self.db.execute(
            "SELECT intent_key,task,task_version,member_ref,state,receipt "
            "FROM intent_ledger WHERE intent_key=?", (intent_key,)).fetchone()
        if not r:
            return None
        keys = ("intent_key", "task", "task_version", "member_ref", "state", "receipt")
        d = dict(zip(keys, r))
        d["receipt"] = json.loads(d["receipt"]) if d["receipt"] else None
        return d
