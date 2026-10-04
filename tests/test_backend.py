import json
from pathlib import Path
import tempfile
import unittest

from personal_memory.archive import Archive
from personal_memory.backend import Hindsight
from personal_memory.model import Document


class BackendTests(unittest.TestCase):
    def test_async_resume_identity_and_failure_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Archive(Path(directory)/"a.db")
            d=Document("s","k","text")
            archive.ingest([d])
            row=archive.pending()[0]
            calls=[]
            class Backend(Hindsight):
                def request(self,suffix,payload=None,method=None):
                    calls.append((suffix,payload))
                    return {} if payload else {"status":"completed"}
            backend=Backend("http://unused","bank",async_retain=True,poll_interval=0)
            backend.retain(row)
            backend.retain(row)
            posts=[p for s,p in calls if p]
            self.assertEqual(posts[0]["operation_id"],posts[1]["operation_id"])
            self.assertEqual(posts[0]["items"][0]["document_id"],d.id)
            class Failed(Backend):
                def request(self,suffix,payload=None,method=None):
                    calls.append((suffix,payload))
                    return {} if payload else {"status":"failed"}
            with self.assertRaises(RuntimeError):
                archive.push(Failed("http://unused","bank",async_retain=True,poll_interval=0))
            self.assertEqual(archive.stats()["pending"],1)
