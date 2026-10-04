import argparse
import json
from pathlib import Path

from .archive import Archive
from .backend import Hindsight
from .sources import codex, hermes, markdown


def main():
    parser = argparse.ArgumentParser(description="Traceable personal memory bridge")
    parser.add_argument("--archive",default="data/archive.db")
    sub = parser.add_subparsers(dest="command",required=True)
    md = sub.add_parser("markdown")
    md.add_argument("root")
    md.add_argument("--source",required=True)
    md.add_argument("--include",action="append",required=True,help="Explicit relative glob allowlist")
    cx = sub.add_parser("codex")
    cx.add_argument("files",nargs="+")
    cx.add_argument("--source",default="codex")
    hm = sub.add_parser("hermes")
    hm.add_argument("database")
    hm.add_argument("--session",action="append",required=True)
    hm.add_argument("--source",default="hermes")
    for cmd in (md,cx,hm):
        cmd.add_argument("--scope",default="personal")
    for action in ("push","recall","mcp","worker"):
        p = sub.add_parser(action)
        p.add_argument("--url",required=True)
        p.add_argument("--bank",required=True)
        p.add_argument("--scope",default="personal")
        if action in ("push","worker"):
            p.add_argument("--limit",type=int,default=20)
            p.add_argument("--async",dest="async_retain",action="store_true",help="Resume the same operation after lost acknowledgement")
            if action=="worker": p.add_argument('--interval',type=int,default=300)
        elif action == "recall":
            p.add_argument("query")
            p.add_argument("--max-tokens",type=int,default=1024)
    export = sub.add_parser("export")
    export.add_argument("directory")
    export.add_argument("--scope",default="personal")
    backup = sub.add_parser("backup")
    backup.add_argument("target")
    sub.add_parser("stats")
    review=sub.add_parser("exclude-memory")
    review.add_argument("document_id")
    review.add_argument("memory_id")
    review.add_argument("--reason",required=True)
    review.add_argument("--scope",default="personal")
    collection=sub.add_parser("collect")
    collection.add_argument("--config",required=True)
    collection.add_argument("--interval",type=int,help="Poll continuously; minimum 30 seconds")
    args = parser.parse_args()
    archive = Archive(args.archive)
    if args.command == "exclude-memory":
        archive.exclude_memory(args.memory_id,args.document_id,args.reason,args.scope)
        result={"excluded":args.memory_id,"model_calls":0}
    elif args.command == "collect":
        from .collector import collect,watch
        config=json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.interval:
            watch(archive,config,args.interval,lambda r: print(json.dumps(r,ensure_ascii=False),flush=True))
            return
        result=collect(archive,config)
    elif args.command == "markdown":
        result = archive.ingest(markdown(args.root,args.source,scope=args.scope,include=args.include))
    elif args.command == "codex":
        result = archive.ingest(d for f in args.files for d in codex(f,args.source,scope=args.scope))
    elif args.command == "hermes":
        result = archive.ingest(hermes(args.database,args.source,session_ids=args.session,scope=args.scope))
    elif args.command in ("push","recall","mcp","worker"):
        backend = Hindsight(args.url,args.bank,scope=args.scope,
                            async_retain=args.command=='worker' or getattr(args,"async_retain",False))
        if args.command=='worker':
            from .worker import watch
            watch(archive,backend,args.scope,args.interval,args.limit,
                  lambda r:print(json.dumps(r),flush=True))
            return
        if args.command == "mcp":
            from .service import MemoryService,mcp_server
            mcp_server(MemoryService(archive,backend,args.scope)).run(transport="stdio")
            return
        if args.command == "push":
            result = archive.push(backend,scope=args.scope,limit=args.limit)
        else:
            from .service import MemoryService
            result = MemoryService(archive,backend,args.scope).search(args.query,args.max_tokens)
    elif args.command == "export":
        result = {"exported":archive.export(args.directory,args.scope)}
    elif args.command == "backup":
        archive.backup(args.target)
        result = {"backup":str(Path(args.target))}
    else:
        result = archive.stats()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
