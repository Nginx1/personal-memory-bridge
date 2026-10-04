"""Zero-model sync polling with compact private status and bounded transport."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True)
    p.add_argument('--status',required=True)
    p.add_argument('--interval',type=int,default=300)
    args=p.parse_args()
    if args.interval<30: raise ValueError('Polling interval must be at least 30 seconds')
    status=Path(args.status)
    status.parent.mkdir(parents=True,exist_ok=True)
    while True:
        record={'at':datetime.now(timezone.utc).isoformat(),'ok':False,'model_calls':0}
        try:
            result=subprocess.run([sys.executable,'-m','personal_memory.sync','push','--config',args.config],
                capture_output=True,text=True,encoding='utf-8',timeout=180)
            if result.returncode==0:
                record.update({'ok':True,'result':json.loads(result.stdout)})
            else: record['error']='Source sync failed; inspect source configuration and SSH availability'
        except (OSError,subprocess.TimeoutExpired,ValueError):
            record['error']='Source sync failed; inspect source configuration and SSH availability'
        temp=status.with_suffix('.tmp'); temp.write_text(json.dumps(record)); temp.replace(status)
        time.sleep(args.interval)


if __name__=='__main__': main()
