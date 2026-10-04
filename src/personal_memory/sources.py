"""Read-only source adapters. No model calls; no internal reasoning ingestion."""
from datetime import datetime, timezone
from contextlib import closing
import fnmatch
import json
from pathlib import Path
import sqlite3

from .model import Document, Redactor


class SourceError(ValueError):
    pass


def cutoff(value):
    if value is None:
        return None
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise SourceError('Collection cutoff must include a timezone')
    return at.timestamp()


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(x.get("text", "") for x in value if isinstance(x, dict)
                         and x.get("type") in ("input_text", "output_text", "text"))
    return ""


def markdown(root, source, *, scope="personal", include=("**",),
             exclude=(".obsidian/**", ".git/**", ".trash/**", "_memory/**"),
             max_bytes=64000, redactor=None, since=None):
    root = Path(root).resolve()
    redact = redactor or Redactor()
    after = cutoff(since)
    for path in sorted(root.rglob("*.md")):
        key = path.relative_to(root).as_posix()
        if (path.is_symlink() or root not in path.resolve().parents
                or any(fnmatch.fnmatchcase(key, p) for p in exclude)
                or not any(fnmatch.fnmatchcase(key, p) for p in include)):
            continue
        if after is not None and path.stat().st_mtime < after:
            continue
        if path.stat().st_size > max_bytes:
            raise SourceError(f"Markdown exceeds configured limit: {key}")
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError as e:
            raise SourceError(f"Markdown is not UTF-8: {key}") from e
        if content.strip():
            yield Document(source, key, redact(content), scope,
                           {"kind": "markdown", "evidence": "source-recorded", "locator": key})


def _turns(messages, source, session, scope, redact, locator, *, since=None, complete_only=False):
    current = []
    start = None
    after = cutoff(since)
    def build(sealed=False):
        if after is not None and (current[0]['at']=='unknown' or cutoff(current[0]['at']) < after):
            return None
        if complete_only and not (sealed or any(m['role']=='assistant' for m in current)):
            return None
        # Status labels are deliberately inside content as well as metadata.
        sections = [f"[{m['role']}; evidence={m['evidence']}; at={m['at']}]\n{redact(m['text'])}"
                    for m in current if m["text"].strip()]
        metadata = {"kind": "conversation", "session": session, "turn": str(start),
                         "locator": locator, "evidence": "mixed-claims",
                         "warning": "Assistant statements are claims; an exit code alone does not verify task completion."}
        if current[-1]["at"] != "unknown":
            metadata["timestamp"] = current[-1]["at"]
        return Document(source, session + ":" + str(start), "\n\n".join(sections), scope, metadata)
    for m in messages:
        if m["role"] == "user":
            if current:
                doc=build(sealed=True)
                if doc is not None: yield doc
            current, start = [], m["id"]
        if start is not None:
            current.append(m)
    if current:
        doc=build()
        if doc is not None: yield doc


def codex(path, source="codex", *, scope="personal", redactor=None, receipt_max_bytes=1200,
          since=None, complete_only=False):
    path = Path(path)
    rows = []
    after = cutoff(since)
    # Reading only newline-terminated rows avoids checkpointing a partial live write.
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.endswith("\n"):
                break
            try:
                row=json.loads(line)
                # Omit old payloads before retaining them in memory. The session
                # identity is always kept; incomplete timestamps fail closed.
                if row.get('type')=='session_meta' or after is None or (
                    row.get('timestamp') and cutoff(row['timestamp']) >= after):
                    rows.append((line_no,row))
            except json.JSONDecodeError as e:
                raise SourceError(f"Invalid JSONL at line {line_no}") from e
    meta = next((r["payload"] for _, r in rows if r.get("type") == "session_meta"), None)
    if not meta or not meta.get("id"):
        raise SourceError("Codex session_meta.id is missing; unsupported log format")
    # Child agents repeat parent context. Collect root conversations only.
    if isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
        return
    messages = []
    for line_no, r in rows:
        if r.get("type") != "response_item":
            continue
        p = r.get("payload", {})
        if p.get("type") == "function_call_output":
            output=p.get("output")
            if isinstance(output,str) and len(output.encode()) <= receipt_max_bytes:
                try:
                    receipt=json.loads(output)
                except json.JSONDecodeError:
                    receipt=None
                if isinstance(receipt,dict) and isinstance(receipt.get("exit_code"),int):
                    messages.append({"id":p.get("call_id",f"line-{line_no}"),"role":"tool-receipt",
                        "text":output,"at":r.get("timestamp","unknown"),"evidence":"observed-untrusted-receipt"})
            continue
        if p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
            continue
        if p.get("role") == "assistant" and (p.get("phase") or p.get("channel")) in ("analysis", "commentary"):
            continue
        value = text_content(p.get("content"))
        # Runtime context and instructions are not personal authored statements.
        if p.get("role") == "user" and value.lstrip().startswith(("<environment_context>", "# AGENTS.md", "<permissions instructions>")):
            continue
        if value:
            messages.append({"id": p.get("id") or f"line-{line_no}", "role": p["role"],
                             "text": value, "at": r.get("timestamp", "unknown"),
                             "evidence": "user-stated" if p["role"] == "user" else "assistant-claimed"})
    # event_msg duplicates response_item. Fail clearly rather than silently consuming
    # a future event-only format or claiming a successful empty import.
    if not messages and any(r.get("type") == "event_msg" and r.get("payload", {}).get("type") in
                            ("user_message", "agent_message") for _, r in rows):
        raise SourceError("Event-only Codex conversation needs a format-specific adapter")
    yield from _turns(messages, source, meta["id"], scope, redactor or Redactor(), path.name,
                      since=since,complete_only=complete_only)


def hermes(path, source="hermes", *, session_ids, scope="personal", redactor=None,
           since=None, complete_only=False):
    after=cutoff(since)
    if not session_ids and after is None:
        raise SourceError("An explicit Hermes session allowlist is required")
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        required = {"id", "session_id", "role", "content", "timestamp"}
        if not required <= columns:
            raise SourceError("Unsupported Hermes messages schema")
        # Stable read transaction protects consistency while the live DB changes.
        db.execute("BEGIN")
        extra = " AND active=1" if "active" in columns else ""
        if not session_ids:
            session_ids=[r[0] for r in db.execute("SELECT DISTINCT session_id FROM messages WHERE role='user' AND timestamp>=?"+extra,(after,))]
        for session in session_ids:
            window = ' AND timestamp>=?' if after is not None else ''
            parameters=(session,after) if after is not None else (session,)
            messages = []
            for msg_id, role, value, timestamp in db.execute(
                "SELECT id,role,content,timestamp FROM messages WHERE session_id=?" + extra + window +
                " AND role IN ('user','assistant') ORDER BY id", parameters):
                value = text_content(value)
                if value:
                    at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                    messages.append({"id": msg_id, "role": role, "text": value, "at": at,
                                     "evidence": "user-stated" if role == "user" else "assistant-claimed"})
            yield from _turns(messages, source, session, scope, redactor or Redactor(), f"session:{session}",
                              since=since,complete_only=complete_only)
