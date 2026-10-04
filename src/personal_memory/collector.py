"""Config-driven local polling, with no model calls during collection."""
import json
from pathlib import Path
import time

from .sources import codex,hermes,markdown,cutoff


def iter_sources(config,feed=None):
    sources=config.get("sources",[])
    if not sources:
        raise ValueError("At least one explicit source is required")
    for source in sources:
        kind=source["kind"]
        name=source["name"]
        scope=source.get("scope","personal")
        window={'since':source.get('since')}
        if kind=="markdown":
            include=source.get("include")
            if not include:
                raise ValueError("Markdown requires an include allowlist")
            docs=markdown(source["path"],name,scope=scope,include=include,
                          **window,
                          exclude=source.get("exclude",[".obsidian/**",".git/**",".trash/**","_memory/**"]))
        elif kind=="codex":
            files=source.get("files",[])
            roots=source.get("roots",[])
            if not files and not roots:
                raise ValueError("Codex requires explicit files or roots")
            paths=set(Path(p) for p in files)
            for root in roots:
                paths.update(Path(root).rglob("*.jsonl"))
            after=cutoff(source.get('since'))
            def codex_docs(paths=sorted(paths),name=name,scope=scope,window=window,source=source,after=after):
                for p in paths:
                    if after is not None and p.stat().st_mtime<after: continue
                    fingerprint=feed.changed(name+':'+str(p),[p]) if feed else None
                    if feed and fingerprint is None: continue
                    documents=list(codex(p,name,scope=scope,**window,complete_only=source.get('complete_only',False)))
                    if feed:
                        feed.publish(documents)
                        feed.scanned(name+':'+str(p),fingerprint)
                    yield from documents
            docs=codex_docs()
        elif kind=="hermes":
            locator=name+':'+source['path']
            fingerprint=feed.changed(locator,[Path(source['path']),Path(source['path']+'-wal')]) if feed else None
            def hermes_docs(source=source,name=name,scope=scope,window=window,locator=locator,fingerprint=fingerprint):
                if feed and fingerprint is None: return
                documents=list(hermes(source["path"],name,scope=scope,session_ids=source.get("sessions",[]),
                               **window,complete_only=source.get('complete_only',False)))
                if feed:
                    feed.publish(documents)
                    feed.scanned(locator,fingerprint)
                yield from documents
            docs=hermes_docs()
        else:
            raise ValueError("Unsupported source kind")
        yield name,scope,docs


def collect(archive, config):
    result=[]
    for name,scope,docs in iter_sources(config):
        result.append({"source":name,"scope":scope,**archive.ingest(docs)})
    return {"sources":result,"archive":archive.stats(),"model_calls":0}


def watch(archive, config, interval, emit):
    if interval < 30:
        raise ValueError("Polling interval must be at least 30 seconds")
    # Model inference is a separate, explicitly configured worker. This poller
    # accumulates changed content without charging on every conversation turn.
    while True:
        emit(collect(archive,config))
        time.sleep(interval)
