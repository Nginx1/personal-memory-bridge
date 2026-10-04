import json
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Hindsight:
    """One fixed bank/scope per service instance. No Reflect on routine lookup."""
    def __init__(self, url, bank, *, scope="personal", api_key=None, timeout=180,
                 async_retain=False, operation_timeout=900, poll_interval=3):
        self.url, self.bank, self.scope = url.rstrip("/"),bank,scope
        self.api_key, self.timeout = api_key,timeout
        self.async_retain=async_retain
        self.operation_timeout,self.poll_interval=operation_timeout,poll_interval

    def request(self, suffix, payload=None, method=None):
        headers = {"Content-Type":"application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = Request(self.url+"/v1/default/banks/"+quote(self.bank,safe="")+suffix,
                      data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode(),
                      headers=headers,method=method)
        try:
            with urlopen(req,timeout=self.timeout) as result:
                return json.load(result)
        except HTTPError as e:
            # Don't echo service errors that might include private source text.
            raise RuntimeError(f"Hindsight returned HTTP {e.code}") from None

    def retain(self, row):
        if row["scope"] != self.scope:
            raise ValueError("Source scope differs from configured backend scope")
        metadata = json.loads(row["metadata"])
        item = {"content":row["content"],"document_id":row["id"],"update_mode":"replace",
                "context":row["source"]+". Preserve attribution, negation, time and evidence labels; assistant claims are not verified outcomes.",
                "timestamp": metadata.get("timestamp","unset"),
                "tags":["scope:"+self.scope],
                "metadata":{"source":row["source"],"key":row["key"],"digest":row["digest"],
                            "revision":str(row["revision"]),"scope":self.scope,
                            "evidence":metadata.get("evidence","unknown")}}
        payload={"items":[item],"async":self.async_retain}
        if not self.async_retain:
            return self.request("/memories",payload)
        operation=str(uuid.uuid5(uuid.NAMESPACE_URL,
                                "personal-memory-0.1:"+self.bank+":"+row["id"]+":"+row["digest"]))
        payload["operation_id"]=operation
        self.request("/memories",payload)
        deadline=time.monotonic()+self.operation_timeout
        while time.monotonic()<deadline:
            result=self.request("/operations/"+operation)
            state=result.get("status")
            if state=="completed":
                return {"operation_id":operation,"status":state}
            if state in ("failed","cancelled","not_found"):
                raise RuntimeError(f"Retain operation {operation} is {state}; no automatic requeue")
            time.sleep(self.poll_interval)
        raise TimeoutError(f"Retain operation {operation} still pending; retry with the same source version")

    def recall(self, query, max_tokens=1024):
        if not 128 <= max_tokens <= 2048:
            raise ValueError("Recall budget must be between 128 and 2048 tokens")
        return self.request("/memories/recall",{"query":query,"budget":"low","max_tokens":max_tokens,
                            "tags":["scope:"+self.scope],"tags_match":"all_strict",
                            "types":["world","experience","observation"],
                            "include":{"entities":{"max_tokens":0}}})
