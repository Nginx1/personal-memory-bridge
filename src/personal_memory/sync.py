"""Bounded source-only transfer over caller-configured SSH stdio commands.

Each server SSH key must have a forced export or ingest command and a fixed
source allowlist. Retrieval keys do not grant ingestion. No model calls here.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

from .archive import Archive
from .collector import iter_sources
from .feed import Feed
from .model import Document,Redactor

MAX_PACKET=2_000_000


def export_packet(config,request=None):
    if config.get('state'):
        feed=Feed(config['state'])
        for name,scope,items in iter_sources(config,feed):
            for item in items: feed.publish([item])
        request=request or {}
        return feed.batch(request.get('after',0),request.get('limit',20),request.get('feed'))
    docs=[]
    for name,scope,items in iter_sources(config):
        for item in items:
            docs.append(asdict(item))
            if len(docs)>100:
                raise ValueError('More than 100 documents; narrow collection window')
    packet={'version':1,'documents':docs}
    encode_packet(packet)
    return packet


def encode_packet(packet):
    payload=json.dumps(packet,ensure_ascii=False,separators=(',',':'))
    if len(payload.encode())>MAX_PACKET:
        raise ValueError('Source packet exceeds 2 MB')
    return payload


def ingest_packet(archive,packet,allowed_sources,scope):
    if packet.get('version')!=1 or not isinstance(packet.get('documents'),list) or len(packet['documents'])>100:
        raise ValueError('Unsupported or oversized source packet')
    docs=[]
    redact=Redactor()
    for row in packet['documents']:
        if not isinstance(row,dict) or row.get('source') not in allowed_sources or row.get('scope')!=scope:
            raise ValueError('Source is outside this SSH key authority')
        if any(not isinstance(row.get(k),str) or not row[k] for k in ('source','key','content')):
            raise ValueError('Invalid document')
        if len(row['key'])>1024 or len(row['content'].encode())>64000:
            raise ValueError('Document exceeds limit')
        meta=row.get('metadata')
        if not isinstance(meta,dict) or meta.get('kind') not in ('markdown','conversation'):
            raise ValueError('Unsupported source metadata')
        docs.append(Document(row['source'],row['key'],redact(row['content']),scope,meta))
    result={**archive.ingest(docs),'model_calls':0}
    if 'feed' in packet:
        result.update({'feed':packet['feed'],'ack_cursor':packet['cursor']})
    return result


def checkpoint(path):
    if not path.exists(): return {'after':0}
    text=path.read_text()
    # Earlier zero-document trial used a payload digest. Replaying at cursor
    # zero is safe: receiver hashes and async operation IDs suppress repeats.
    if len(text)==64 and all(x in '0123456789abcdef' for x in text): return {'after':0}
    data=json.loads(text)
    return {'after':data['cursor'],'feed':data['feed']}


def save_checkpoint(path,packet):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps({'feed':packet['feed'],'cursor':packet['cursor']}))
    temp.chmod(0o600); temp.replace(path)


def run_command(command,payload=None):
    if not isinstance(command,list) or not command or any(not isinstance(x,str) for x in command):
        raise ValueError('Transport command must be an argument array')
    result=subprocess.run(command,input=payload,capture_output=True,text=True,encoding='utf-8',timeout=120,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode:
        raise RuntimeError('Source transfer failed; inspect transport separately')
    if len(result.stdout.encode())>MAX_PACKET:
        raise ValueError('Transport response exceeds limit')
    return result.stdout


def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('export','ingest','push','pull'))
    p.add_argument('--config',required=True)
    args=p.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    if args.action=='export':
        request={}
        if config.get('state'):
            data=sys.stdin.buffer.read(1025)
            if len(data)>1024: raise ValueError('Cursor request exceeds limit')
            request=json.loads(data) if data else {}
        print(encode_packet(export_packet(config,request)))
        return
    if args.action=='push':
        cp=Path(config['checkpoint'])
        request=checkpoint(cp)
        packet=export_packet(config,request)
        payload=encode_packet(packet)
        if config.get('state') and request.get('feed')==packet['feed'] and request['after']==packet['cursor']:
            print(json.dumps({'transfer_skipped':True,'model_calls':0}))
            return
        result=json.loads(run_command(config['command'],payload))
        if config.get('state'):
            if result.get('ack_cursor')!=packet['cursor'] or result.get('feed')!=packet['feed']:
                raise ValueError('Receiver acknowledgement differs from sent batch')
            save_checkpoint(cp,packet)
    else:
        if args.action=='ingest':
            data=sys.stdin.buffer.read(MAX_PACKET+1)
            if len(data)>MAX_PACKET: raise ValueError('Source packet exceeds limit')
            payload=data.decode('utf-8')
        else:
            cp=Path(config['checkpoint'])
            payload=run_command(config['command'],json.dumps(checkpoint(cp)))
        packet=json.loads(payload)
        result=ingest_packet(Archive(config['archive']),packet,config['allowed_sources'],config['scope'])
        if args.action=='pull' and 'feed' in packet: save_checkpoint(cp,packet)
    print(json.dumps(result))


if __name__=='__main__': main()
