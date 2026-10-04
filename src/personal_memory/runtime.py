"""Supervise the fixed local API and one cost gate without exposing model keys."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from .budget import Ledger,Tariff
from .proxy import cost_proxy


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    ledger=Ledger(config['ledger'],ceiling_cny=config['ceiling_cny'],max_calls=config['max_calls'])
    llm=config['llm']
    gate,auth=cost_proxy(llm['base_url'],Path(llm['key_file']).read_text().strip(),llm['model'],ledger,
        Tariff(*llm['tariff']),max_output=llm.get('max_output',8192),disable_thinking=llm.get('disable_thinking',False))
    threading.Thread(target=gate.serve_forever,daemon=True).start()
    env={**os.environ,**config.get('environment',{}),
         'HINDSIGHT_API_DATABASE_URL':config['database_url'],
         'HINDSIGHT_API_LLM_PROVIDER':'openai','HINDSIGHT_API_LLM_MODEL':llm['model'],
         'HINDSIGHT_API_LLM_API_KEY':auth,'HINDSIGHT_API_LLM_BASE_URL':f'http://127.0.0.1:{gate.server_port}/v1',
         'HINDSIGHT_API_SKIP_LLM_VERIFICATION':'true',
         'HINDSIGHT_API_LLM_MAX_RETRIES':'0','HINDSIGHT_API_LLM_MAX_CONCURRENT':'1',
         'HINDSIGHT_API_WORKER_MAX_RETRIES':'0'}
    child=subprocess.Popen([sys.executable,'-c','from hindsight_api.main import main; main()',
                             '--host','127.0.0.1','--port',str(config.get('port',8889))],env=env)
    def stop(signum,frame):
        child.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        code=child.wait()
    finally:
        if child.poll() is None: child.terminate()
        gate.shutdown(); gate.server_close()
    raise SystemExit(code)


if __name__=='__main__': main()
