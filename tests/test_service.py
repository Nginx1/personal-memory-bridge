import json
from pathlib import Path
import tempfile
import unittest

from personal_memory.archive import Archive
from personal_memory.model import Document
from personal_memory.service import MemoryService


class ServiceTests(unittest.TestCase):
    def test_review_exclusion_is_version_bound_and_survives_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Archive(Path(directory)/"a.db")
            doc=Document("s","note","助手报告未核实",metadata={"evidence":"mixed-claims"})
            archive.ingest([doc])
            with archive.connection() as db:
                db.execute('UPDATE documents SET pushed_digest=digest')
            class Backend:
                scope="personal"
                def recall(self,query,max_tokens):
                    return {"results":[{"id":"incorrect","text":"无依据的补写","document_id":doc.id,
                        "metadata":{"scope":"personal","digest":doc.digest}}]}
            archive.exclude_memory("incorrect",doc.id,"原文没有这项推断")
            result=MemoryService(archive,Backend()).search("查询")
            self.assertEqual(result['memories'],[])
            self.assertEqual(result['review_excluded'],1)
            archive.backup(Path(directory)/"b.db")
            restored=Archive(Path(directory)/"b.db")
            self.assertTrue(restored.memory_excluded("incorrect",doc.id,doc.digest))
            self.assertFalse(restored.memory_excluded("incorrect",doc.id,"new-digest"))
            with self.assertRaises(ValueError):
                archive.exclude_memory("incorrect","out-of-scope","bad")

    def test_semantic_ranking_and_compact_result_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Archive(Path(directory)/"a.db")
            docs=[Document("s",str(i),str(i)) for i in range(6)]
            archive.ingest(docs)
            with archive.connection() as db:
                db.execute('UPDATE documents SET pushed_digest=digest')
            class Backend:
                scope="personal"
                def recall(self,query,max_tokens):
                    return {"results":[{"text":d.content,"document_id":d.id,
                          "metadata":{"scope":"personal","digest":d.digest},
                          "scores":{"semantic":i/10,"final":10-i}} for i,d in enumerate(docs)]}
            result=MemoryService(archive,Backend()).search("查询")
            self.assertEqual([x["text"] for x in result["memories"]],["5","4","3","2"])
    def test_sources_and_stale_or_wrong_scope_memories(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Archive(Path(directory)/"a.db")
            d=Document("s","note","有来源的笔记",metadata={"evidence":"authored"})
            archive.ingest([d])
            with archive.connection() as db:
                db.execute('UPDATE documents SET pushed_digest=digest')
            class Backend:
                scope="personal"
                def recall(self,query,max_tokens):
                    return {"results":[
                        {"text":"正确","document_id":d.id,"metadata":{"scope":"personal","digest":d.digest}},
                        {"text":"过时","document_id":d.id,"metadata":{"scope":"personal","digest":"old"}},
                        {"text":"跨范围","document_id":d.id,"metadata":{"scope":"work","digest":d.digest}},
                        {"text":"无来源","document_id":"missing","metadata":{}}]}
            service=MemoryService(archive,Backend())
            result=service.search("查询")
            self.assertEqual([x["text"] for x in result["memories"]],["正确"])
            self.assertEqual(result["stale_omitted"],1)
            self.assertEqual(result["unavailable_omitted"],2)
            self.assertEqual(service.source(d.id)["content"],d.content)
            self.assertFalse(service.source("unknown")["found"])
            # Partial backend facts must not escape while the local outbox
            # still lacks acknowledgement for this complete source version.
            with archive.connection() as db:
                db.execute('UPDATE documents SET pushed_digest=NULL')
            partial=service.search("查询")
            self.assertEqual(partial["memories"],[])
            self.assertEqual(partial["unavailable_omitted"],4)
