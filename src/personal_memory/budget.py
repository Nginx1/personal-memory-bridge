"""Atomic reservations in micro-CNY, including attempts with unknown usage."""
from decimal import Decimal, ROUND_CEILING
from contextlib import contextmanager,closing
import json
from pathlib import Path
import sqlite3
import uuid


class BudgetExceeded(RuntimeError):
    pass


def micro_cny(value):
    return int((Decimal(str(value)) * 1000000).to_integral_value(rounding=ROUND_CEILING))


class Ledger:
    @classmethod
    def extend_attempt_limit(cls,path,new_limit,reason):
        """Explicit stage transition; never resets spending or raises the CNY cap."""
        if not reason.strip():
            raise ValueError('A stage review reason is required')
        with closing(sqlite3.connect(path,timeout=30)) as db,db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT ceiling,max_calls FROM policy WHERE id=1').fetchone()
            if row is None or new_limit<=row[1]:
                raise ValueError('New attempt limit must extend an existing policy')
            db.execute('CREATE TABLE IF NOT EXISTS policy_changes(old_max_calls INTEGER,new_max_calls INTEGER,reason TEXT,timestamp TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO policy_changes(old_max_calls,new_max_calls,reason) VALUES(?,?,?)',(row[1],new_limit,reason))
            db.execute('UPDATE policy SET max_calls=? WHERE id=1',(new_limit,))

    def __init__(self, path, *, ceiling_cny, max_calls):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if Decimal(str(ceiling_cny)) <= 0 or max_calls < 1:
            raise ValueError("Positive budget and request limit required")
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS policy(
              id INTEGER PRIMARY KEY CHECK(id=1), ceiling INTEGER, max_calls INTEGER);
            CREATE TABLE IF NOT EXISTS calls(
              id TEXT PRIMARY KEY, state TEXT, reserved INTEGER, charged INTEGER,
              model TEXT, usage TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
            """)
            values = (micro_cny(ceiling_cny),max_calls)
            db.execute("INSERT OR IGNORE INTO policy VALUES(1,?,?)", values)
            if tuple(db.execute("SELECT ceiling,max_calls FROM policy WHERE id=1").fetchone()) != values:
                raise ValueError("Existing ledger policy differs; explicit budget migration required")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, model, amount):
        if amount <= 0:
            raise ValueError("Unknown/zero reservation is not allowed")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM calls WHERE state='accounting-error'").fetchone()[0]:
                raise BudgetExceeded("Accounting error requires review before more requests")
            ceiling, max_calls = db.execute("SELECT ceiling,max_calls FROM policy").fetchone()
            used, count = db.execute("SELECT coalesce(sum(coalesce(charged,reserved)),0),count(*) FROM calls").fetchone()
            if used + amount > ceiling or count >= max_calls:
                raise BudgetExceeded("Cost or request ceiling reached")
            call = str(uuid.uuid4())
            db.execute("INSERT INTO calls(id,state,reserved,model) VALUES(?,?,?,?)",
                       (call,"reserved",amount,model))
            return call

    def settle(self, call, charge=None, usage=None):
        with self.connect() as db:
            row = db.execute("SELECT reserved,state FROM calls WHERE id=?", (call,)).fetchone()
            if not row or row[1] != "reserved":
                raise ValueError("Reservation missing or already settled")
            if charge is not None and (charge < 0 or charge > row[0]):
                # Preserve the high-water estimate and flag underestimated accounting.
                db.execute("UPDATE calls SET state='accounting-error',charged=? WHERE id=?",
                           (max(charge,row[0]),call))
                db.commit()
                raise ValueError("Reported cost exceeded reservation; stop the pilot")
            db.execute("UPDATE calls SET state=?,charged=?,usage=? WHERE id=?",
                       ("known" if charge is not None else "unknown", charge if charge is not None else row[0],
                        json.dumps(usage or {}),call))

    def stats(self):
        with self.connect() as db:
            used,count = db.execute("SELECT coalesce(sum(coalesce(charged,reserved)),0),count(*) FROM calls").fetchone()
            unknown = db.execute("SELECT count(*) FROM calls WHERE state!='known'").fetchone()[0]
            return {"requests":count,"accounted_cny":used/1000000,"unknown_requests":unknown,
                    "cost_note":"Estimate from declared tariff; unknown attempts keep full reservation. Supplier billing is authoritative."}


class Tariff:
    def __init__(self, input_per_million, output_per_million, cached_per_million=None):
        self.input = Decimal(str(input_per_million))
        self.output = Decimal(str(output_per_million))
        self.cached = self.input if cached_per_million is None else Decimal(str(cached_per_million))
        if min(self.input,self.output,self.cached) < 0 or not self.input or not self.output:
            raise ValueError("Verified positive tariff required")

    def cost(self, prompt, completion, cached=0):
        if min(prompt,completion,cached) < 0 or cached > prompt:
            raise ValueError("Invalid provider usage")
        # completion_tokens already includes reasoning_tokens; never add twice.
        return int(((prompt-cached)*self.input + cached*self.cached + completion*self.output)
                   .to_integral_value(rounding=ROUND_CEILING))
