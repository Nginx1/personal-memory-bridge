"""Wait for local database/model readiness without querying the model API."""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--url',required=True)
    p.add_argument('--timeout',type=int,default=120)
    args=p.parse_args()
    deadline=time.monotonic()+args.timeout
    while time.monotonic()<deadline:
        try:
            with urlopen(args.url.rstrip('/')+'/health',timeout=3) as response:
                if json.load(response).get('status')=='healthy': return
        except (URLError,OSError,ValueError): pass
        time.sleep(2)
    raise SystemExit('Personal memory API did not become healthy')


if __name__=='__main__': main()
