import concurrent.futures
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from personal_memory.archive import Archive
from personal_memory.budget import BudgetExceeded, Ledger, Tariff
from personal_memory.model import Document, Redactor
from personal_memory.sources import SourceError, codex, hermes, markdown


class CoreTests(unittest.TestCase):
    def test_explicit_attempt_extension_preserves_spend_and_currency_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'l.db'
            ledger=Ledger(path,ceiling_cny=1,max_calls=1)
            call=ledger.reserve('model',1000)
            ledger.settle(call)
            with self.assertRaises(BudgetExceeded): ledger.reserve('model',1000)
            Ledger.extend_attempt_limit(path,2,'Reviewed next bounded deployment stage')
            extended=Ledger(path,ceiling_cny=1,max_calls=2)
            self.assertEqual(extended.stats()['accounted_cny'],.001)
            extended.reserve('model',1000)
            with self.assertRaises(BudgetExceeded): extended.reserve('model',1000)
            with self.assertRaises(ValueError): Ledger(path,ceiling_cny=2,max_calls=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_mount_independent_identity_and_incremental_update(self):
        vault = self.root / "vault"
        vault.mkdir()
        p = vault / "偏好.md"
        p.write_text("我喜欢简短答复。",encoding="utf-8")
        other = self.root / "other-mount"
        other.mkdir()
        (other / p.name).write_text(p.read_text(encoding="utf-8"),encoding="utf-8")
        a = Archive(self.root / "a.db")
        self.assertEqual(a.ingest(markdown(vault,"logical-vault"))["added"],1)
        self.assertEqual(a.ingest(markdown(other,"logical-vault"))["skipped"],1)
        p.write_text("复杂问题要充分说明。",encoding="utf-8")
        self.assertEqual(a.ingest(markdown(vault,"logical-vault"))["updated"],1)
        self.assertEqual(a.stats()["revisions"],2)

    def test_generated_and_private_folders_excluded(self):
        for name in ["_memory/out.md",".obsidian/a.md","notes/allowed.md","private/x.md"]:
            p = self.root/name
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text("笔记",encoding="utf-8")
        self.assertEqual([d.key for d in markdown(self.root,"v",include=("notes/**",))],["notes/allowed.md"])

    def test_archive_transaction_rolls_back_on_parse_failure(self):
        a = Archive(self.root/"a.db")
        def bad():
            yield Document("s","k","partial")
            raise SourceError("corrupt")
        with self.assertRaises(SourceError):
            a.ingest(bad())
        self.assertEqual(a.stats()["documents"],0)

    def test_failed_backend_and_concurrent_updates_stay_pending(self):
        a = Archive(self.root/"a.db")
        d = Document("s","k","first")
        a.ingest([d])
        class Failing:
            def retain(self,row):
                raise RuntimeError("timeout")
        with self.assertRaises(RuntimeError):
            a.push(Failing())
        self.assertEqual(a.stats()["pending"],1)
        class Updating:
            def retain(self,row):
                a.ingest([Document("s","k","new")])
        a.push(Updating())
        self.assertEqual(a.stats()["pending"],1)

    def test_backup_export_and_scope_boundaries(self):
        a = Archive(self.root/"a.db")
        one = Document("s","../../escape","visible")
        two = Document("s","secret","private",scope="work")
        a.ingest([one,two])
        self.assertIsNone(a.get(two.id))
        self.assertEqual(a.export(self.root/"export"),1)
        self.assertTrue((self.root/"export"/(one.id+".md")).exists())
        a.backup(self.root/"backup.db")
        self.assertEqual(Archive(self.root/"backup.db").get(one.id)["content"],"visible")

    def write_log(self,records):
        p=self.root/"session.jsonl"
        p.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in records),encoding="utf-8")
        return p

    def test_codex_duplicates_reasoning_final_and_repeated_prompts(self):
        records=[{"type":"session_meta","payload":{"id":"s","source":"vscode"}}]
        for i in range(2):
            records += [
                {"type":"event_msg","payload":{"type":"user_message","message":"相同问题"}},
                {"type":"response_item","payload":{"type":"message","role":"user","id":f"u{i}","content":[{"type":"input_text","text":"相同问题"}]}},
                {"type":"response_item","payload":{"type":"reasoning","summary":"never import"}},
                {"type":"response_item","payload":{"type":"message","role":"assistant","channel":"analysis","content":[{"type":"output_text","text":"private chain"}]}},
                {"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"只是建议，尚未验证"}]}}]
        docs=list(codex(self.write_log(records)))
        self.assertEqual(len(docs),2)
        self.assertNotEqual(docs[0].id,docs[1].id)
        self.assertEqual(docs[0].content.count("相同问题"),1)
        self.assertNotIn("private chain",docs[0].content)
        self.assertIn("assistant-claimed",docs[0].content)

    def test_live_partial_tail_then_completion(self):
        p=self.write_log([{"type":"session_meta","payload":{"id":"s"}}])
        with p.open("a",encoding="utf-8") as f:
            f.write('{"type":"response_item","payload":{"type":"message","role":"user","content":"hi"}}')
        self.assertEqual(list(codex(p)),[])
        with p.open("a",encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual(len(list(codex(p))),1)

    def test_bounded_work_receipts_are_observations_not_success_claims(self):
        p=self.write_log([{"type":"session_meta","payload":{"id":"s"}},
          {"type":"response_item","payload":{"type":"message","role":"user","content":"检查结果"}},
          {"type":"response_item","payload":{"type":"function_call_output","call_id":"call1","output":json.dumps({"exit_code":0,"stdout":"process status unknown"})}},
          {"type":"response_item","payload":{"type":"function_call_output","call_id":"call2","output":"very large tool output"*1000}}])
        doc=list(codex(p))[0]
        self.assertIn("observed-untrusted-receipt",doc.content)
        self.assertNotIn("very large tool output",doc.content)
        self.assertIn("does not verify task completion",doc.metadata["warning"])

    def test_schema_drift_fails_clearly_and_subagent_is_skipped(self):
        p=self.write_log([{"type":"session_meta","payload":{"id":"s"}},
                          {"type":"event_msg","payload":{"type":"user_message","message":"hi"}}])
        with self.assertRaises(SourceError):
            list(codex(p))
        p=self.write_log([{"type":"session_meta","payload":{"id":"s","source":{"subagent":{"thread":"parent"}}}}])
        self.assertEqual(list(codex(p)),[])

    def test_hermes_read_only_active_messages_and_reasoning_excluded(self):
        p=self.root/"hermes.db"
        with closing(sqlite3.connect(p)) as db, db:
            db.executescript("CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL,active INTEGER,reasoning TEXT)")
            for row in [(1,"s","user","数据库问题",1,1,"private"),
                        (2,"s","assistant","已解决的说法未验证",2,1,"private"),
                        (3,"s","assistant","旧分支",3,0,"private"),
                        (4,"s","tool","bulk",4,1,"private")]:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",row)
        before=p.read_bytes()
        doc=list(hermes(p,session_ids=["s"]))[0]
        self.assertNotIn("private",doc.content)
        self.assertNotIn("旧分支",doc.content)
        self.assertEqual(p.read_bytes(),before)
        with self.assertRaises(SourceError):
            list(hermes(p,session_ids=[]))

    def test_credentials_removed(self):
        redactor=Redactor(["an-explicit-credential"])
        # Synthetic marker with deliberately invalid key material; no key is
        # embedded in the repository or generated release artifacts.
        begin='-'*5+'BEGIN OPENSSH '+'PRIVATE KEY'+'-'*5
        end='-'*5+'END OPENSSH '+'PRIVATE KEY'+'-'*5
        text="密码是abc.123\napi_key=some-secret\nBearer mytoken\nsk-abcdefghijklmnop an-explicit-credential\n"+begin+'\nkeymaterial\n'+end
        out=redactor(text)
        for secret in ["abc.123","some-secret","mytoken","sk-abcdefghijklmnop","an-explicit-credential","keymaterial"]:
            self.assertNotIn(secret,out)

    def test_atomic_parallel_reservations_and_unknown_cost(self):
        ledger=Ledger(self.root/"ledger.db",ceiling_cny=1,max_calls=10)
        def reserve(_):
            try:
                return ledger.reserve("m",600000)
            except BudgetExceeded:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            calls=[x for x in pool.map(reserve,range(4)) if x]
        self.assertEqual(len(calls),1)
        ledger.settle(calls[0])
        self.assertEqual(ledger.stats()["accounted_cny"],.6)
        self.assertEqual(ledger.stats()["unknown_requests"],1)

    def test_retries_call_limit_and_completion_usage_accounting(self):
        ledger=Ledger(self.root/"ledger.db",ceiling_cny=200,max_calls=1)
        call=ledger.reserve("m",1000000)
        tariff=Tariff(9,27,.3)
        self.assertEqual(tariff.cost(1000,100,400),8220)
        ledger.settle(call,tariff.cost(1000,100,400),{"completion_tokens":100,"completion_tokens_details":{"reasoning_tokens":50}})
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("m",1)
        with self.assertRaises(ValueError):
            Ledger(self.root/"ledger.db",ceiling_cny=201,max_calls=1)

    def test_underestimated_usage_stops_future_calls(self):
        ledger=Ledger(self.root/"l.db",ceiling_cny=200,max_calls=10)
        call=ledger.reserve("m",1)
        with self.assertRaises(ValueError):
            ledger.settle(call,10)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("m",1)


if __name__ == "__main__":
    unittest.main()
