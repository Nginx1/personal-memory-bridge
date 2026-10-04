from pathlib import Path
from dataclasses import asdict
import json
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from personal_memory.archive import Archive
from personal_memory.collector import collect
from personal_memory.model import Document
from personal_memory.sources import codex,hermes
from personal_memory.sync import ingest_packet
from personal_memory.sync import export_packet
from personal_memory.feed import Feed


class CollectorTests(unittest.TestCase):
    def test_feed_replay_pagination_and_reset_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            feed=Feed(Path(directory)/'feed.db')
            docs=[Document('allowed',str(i),'fact '+str(i),'work',{'kind':'markdown'}) for i in range(131)]
            feed.publish(docs)
            first=feed.batch(limit=20)
            self.assertTrue(first['more'])
            self.assertEqual(len(first['documents']),20)
            self.assertEqual(feed.batch(limit=20),first)
            archive=Archive(Path(directory)/'receiver.db')
            self.assertEqual(ingest_packet(archive,first,['allowed'],'work')['added'],20)
            self.assertEqual(ingest_packet(archive,first,['allowed'],'work')['skipped'],20)
            cursor=first['cursor']
            while True:
                batch=feed.batch(cursor,20,first['feed'])
                ingest_packet(archive,batch,['allowed'],'work')
                cursor=batch['cursor']
                if not batch['more']: break
            self.assertEqual(archive.stats()['documents'],131)
            feed.publish(docs)
            self.assertEqual(feed.batch(cursor,20,first['feed'])['documents'],[])
            other=Feed(Path(directory)/'reset.db')
            with self.assertRaises(ValueError): other.batch(cursor,20,first['feed'])

    def test_feed_skips_unchanged_codex_files_and_captures_later_final(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); path=root/'s.jsonl'
            records=[{'type':'session_meta','payload':{'id':'s'}},
                {'type':'response_item','timestamp':'2026-02-01T00:00:01Z',
                 'payload':{'type':'message','role':'user','content':'question'}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            config={'state':str(root/'feed.db'),'sources':[{'kind':'codex','name':'codex','files':[str(path)],
                'since':'2026-02-01T00:00:00Z','complete_only':True}]}
            self.assertEqual(export_packet(config)['documents'],[])
            with path.open('a') as f:
                f.write(json.dumps({'type':'response_item','timestamp':'2026-02-01T00:00:02Z',
                    'payload':{'type':'message','role':'assistant','content':'response'}})+'\n')
            packet=export_packet(config)
            self.assertEqual(len(packet['documents']),1)
            with patch('personal_memory.collector.codex',side_effect=AssertionError('Unchanged file was reparsed')):
                self.assertEqual(export_packet(config),packet)
                request={'after':packet['cursor'],'feed':packet['feed']}
                self.assertEqual(export_packet(config,request)['documents'],[])

    def test_new_content_window_omits_history_and_unfinished_codex_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'session.jsonl'
            records=[{'type':'session_meta','payload':{'id':'s'}}]
            for role,text,at in [('user','old','2026-01-01T00:00:00Z'),
                                  ('assistant','old response','2026-01-01T00:00:01Z'),
                                  ('user','new','2026-02-01T00:00:00Z')]:
                records.append({'type':'response_item','timestamp':at,
                    'payload':{'type':'message','role':role,'content':text}})
            p.write_text(''.join(json.dumps(r)+'\n' for r in records))
            self.assertEqual(list(codex(p,since='2026-02-01T00:00:00Z',complete_only=True)),[])
            with p.open('a') as f:
                f.write(json.dumps({'type':'response_item','timestamp':'2026-02-01T00:00:01Z',
                    'payload':{'type':'message','role':'assistant','content':'new response'}})+'\n')
            docs=list(codex(p,since='2026-02-01T00:00:00Z',complete_only=True))
            self.assertEqual(len(docs),1)
            self.assertNotIn('old',docs[0].content)
            self.assertIn('new response',docs[0].content)

    def test_hermes_discovers_only_sessions_with_new_user_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'h.db'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL)')
                db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)',[
                    (1,'old','user','old',1),(2,'old','assistant','late old response',201),
                    (3,'new','user','new',200),(4,'new','assistant','reply',202)])
            docs=list(hermes(path,session_ids=[],since='1970-01-01T00:03:00Z',complete_only=True))
            self.assertEqual([d.metadata['session'] for d in docs],['new'])

    def test_ssh_source_authority_is_atomic_and_redacts_content(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Archive(Path(directory)/'a.db')
            row=asdict(Document('allowed','k','password=not-for-memory','work',{'kind':'markdown'}))
            other={**row,'source':'forbidden'}
            with self.assertRaises(ValueError):
                ingest_packet(archive,{'version':1,'documents':[row,other]},['allowed'],'work')
            self.assertEqual(archive.stats()['documents'],0)
            result=ingest_packet(archive,{'version':1,'documents':[row]},['allowed'],'work')
            self.assertEqual(result['model_calls'],0)
            self.assertNotIn('not-for-memory',archive.pending('work')[0]['content'])

    def test_polling_skips_unchanged_and_retains_updates_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"notes").mkdir()
            note=root/"notes"/"first.md"
            note.write_text("最初版本",encoding="utf-8")
            archive=Archive(root/"archive.db")
            config={"sources":[{"kind":"markdown","name":"vault","path":str(root),"include":["notes/**"]}]}
            self.assertEqual(collect(archive,config)["sources"][0]["added"],1)
            repeat=collect(archive,config)
            self.assertEqual(repeat["sources"][0]["skipped"],1)
            self.assertEqual(repeat["model_calls"],0)
            note.write_text("改动版本",encoding="utf-8")
            self.assertEqual(collect(archive,config)["sources"][0]["updated"],1)
