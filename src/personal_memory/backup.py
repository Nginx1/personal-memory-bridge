"""Private snapshots; pause extraction for a consistent cross-database backup."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from urllib.parse import urlsplit,unquote

from .archive import Archive


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True)
    p.add_argument('--archive',required=True)
    p.add_argument('--directory',required=True)
    args=p.parse_args()
    config=json.loads(Path(args.config).read_text())
    destination=Path(args.directory)/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination.mkdir(parents=True,mode=0o700)
    url=urlsplit(config['database_url'])
    env={**os.environ,'PGPASSWORD':unquote(url.password or '')}
    # The password is never placed in process arguments or the manifest.
    subprocess.run(['pg_dump','-Fc','--no-owner','--no-privileges','-h',url.hostname or 'localhost',
        '-p',str(url.port or 5432),'-U',unquote(url.username or ''),'-d',unquote(url.path[1:]),
        '-f',str(destination/'hindsight.dump')],env=env,check=True,timeout=600)
    Archive(args.archive).backup(destination/'archive.db')
    with closing(sqlite3.connect(config['ledger'])) as source,closing(sqlite3.connect(destination/'costs.db')) as target:
        source.backup(target)
    manifest={'at':datetime.now(timezone.utc).isoformat(),'model':config['llm']['model'],
              'embedding':config.get('environment',{}).get('HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID'),
              'ceiling_cny':config['ceiling_cny'],'max_calls':config['max_calls'],'files':{}}
    for file in destination.iterdir():
        file.chmod(0o600)
        with file.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
        manifest['files'][file.name]={'bytes':file.stat().st_size,'sha256':digest}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (destination/'manifest.json').chmod(0o600)
    print(json.dumps({'backup':str(destination),'files':len(manifest['files'])}))


if __name__=='__main__': main()
