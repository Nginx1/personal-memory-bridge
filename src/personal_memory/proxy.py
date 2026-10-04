"""Loopback OpenAI-compatible cost gate. Prompts/keys/reasoning are not logged."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .budget import BudgetExceeded


def cost_proxy(upstream, api_key, model, ledger, tariff, *, max_output=4096,
               max_body_bytes=100000, port=0, timeout=150, audit=None,
               disable_thinking=False):
    auth = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body):
            if audit:
                audit({"path":self.path,"status":code})
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + auth:
                return self.reply(403, {"error":"Forbidden"})
            call = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > max_body_bytes:
                    return self.reply(413, {"error":"Request exceeds pilot size limit"})
                payload = json.loads(self.rfile.read(size))
                if payload.get("model") != model or payload.get("stream"):
                    return self.reply(400, {"error":"Pilot permits pinned model and non-streaming calls only"})
                # Cap *all* completion budget fields; do not silently allow n>1.
                if payload.get("n",1) != 1:
                    return self.reply(400, {"error":"Only one completion permitted"})
                cap = min(max_output,int(payload.get("max_completion_tokens",payload.get("max_tokens",max_output))))
                if cap < 1:
                    return self.reply(400, {"error":"Invalid completion limit"})
                if "max_completion_tokens" in payload:
                    payload["max_completion_tokens"] = cap
                    payload.pop("max_tokens",None)
                else:
                    payload["max_tokens"] = cap
                # UTF-8 bytes + protocol allowance is a conservative text-token bound.
                # Vision/audio/remote-content requests are not supported by this gate.
                if any(not isinstance(m.get("content"), (str,type(None))) for m in payload.get("messages",[])):
                    return self.reply(400, {"error":"Text-only pilot"})
                if disable_thinking:
                    # Opt in only for a provider/model whose API supports this.
                    # Remove conflicting effort sent by reasoning-model clients.
                    payload["thinking"] = {"type":"disabled"}
                    payload.pop("reasoning_effort",None)
                encoded = json.dumps(payload,ensure_ascii=False).encode()
                prompt_bound = len(encoded)+2048
                call = ledger.reserve(model,tariff.cost(prompt_bound,cap))
                request = Request(upstream.rstrip("/")+"/chat/completions",data=encoded,
                                  headers={"Content-Type":"application/json","Authorization":"Bearer "+api_key})
                with urlopen(request,timeout=timeout) as response:
                    body = response.read()
                parsed = json.loads(body)
                usage = parsed.get("usage",{})
                if "prompt_tokens" in usage and "completion_tokens" in usage:
                    cached = usage.get("prompt_tokens_details",{}).get("cached_tokens",usage.get("prompt_cache_hit_tokens",0))
                    ledger.settle(call,tariff.cost(usage["prompt_tokens"],usage["completion_tokens"],cached),usage)
                else:
                    ledger.settle(call)
                self.reply(200,body)
            except BudgetExceeded:
                self.reply(429, {"error":"Pilot budget exhausted"})
            except HTTPError as error:
                if call:
                    ledger.settle(call,usage={"upstream_http_status":error.code})
                if audit:
                    audit({"upstream_http_status":error.code})
                self.reply(502, {"error":"Upstream rejected request; attempt reserved conservatively"})
            except (URLError,TimeoutError):
                if call:
                    ledger.settle(call,usage={"upstream_error":"transport-or-timeout"})
                self.reply(502, {"error":"Upstream failed; attempt reserved conservatively"})
            except Exception:
                if call:
                    # If accounting failed, the reservation remains charged; never refund.
                    try:
                        ledger.settle(call)
                    except ValueError:
                        pass
                self.reply(500, {"error":"Pilot request failed; inspect local accounting"})

    server = ThreadingHTTPServer(("127.0.0.1",port),Handler)
    server.daemon_threads = True
    return server, auth
