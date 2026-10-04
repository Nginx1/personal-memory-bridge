from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from personal_memory.budget import Ledger,Tariff
from personal_memory.proxy import cost_proxy


class ProxyTests(unittest.TestCase):
    def test_real_http_gate_auth_caps_and_no_extra_attempts(self):
        payloads=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body=json.dumps({"choices":[{"message":{"content":"ok"}}],
                                 "usage":{"prompt_tokens":100,"completion_tokens":50}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
        upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger(Path(directory)/"l.db",ceiling_cny=200,max_calls=1)
            proxy,auth=cost_proxy(f"http://127.0.0.1:{upstream.server_port}/v1","fake-key","pinned",ledger,Tariff(9,27),max_output=100,disable_thinking=True)
            threading.Thread(target=proxy.serve_forever,daemon=True).start()
            def send(auth_value,**extras):
                payload={"model":"pinned","messages":[{"role":"user","content":"中文"}],"max_tokens":500}
                payload.update(extras)
                request=Request(f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",data=json.dumps(payload).encode(),
                                headers={"Authorization":"Bearer "+auth_value,"Content-Type":"application/json"})
                return json.load(urlopen(request,timeout=5))
            try:
                with self.assertRaises(HTTPError) as e:
                    send("wrong")
                self.assertEqual(e.exception.code,403)
                self.assertEqual(ledger.stats()["requests"],0)
                send(auth,thinking={"type":"enabled"},reasoning_effort="high")
                self.assertEqual(payloads[0]["max_tokens"],100)
                self.assertEqual(payloads[0]["thinking"],{"type":"disabled"})
                self.assertNotIn("reasoning_effort",payloads[0])
                with self.assertRaises(HTTPError) as e:
                    send(auth)
                self.assertEqual(e.exception.code,429)
                self.assertEqual(len(payloads),1)
                self.assertEqual(ledger.stats()["accounted_cny"],.00225)
            finally:
                proxy.shutdown()
                proxy.server_close()
                upstream.shutdown()
                upstream.server_close()
