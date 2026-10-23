import json
import urllib.error
import urllib.request
from .runtime_config import settings

def call(action,**kwargs):
    config=settings()
    req=urllib.request.Request(f"http://127.0.0.1:{config['port']}/rpc",data=json.dumps({'action':action,**kwargs}).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+config['token']})
    try:
        with urllib.request.urlopen(req,timeout=150) as response:return json.load(response)['result']
    except urllib.error.HTTPError as exc:raise RuntimeError(exc.read().decode()) from exc
    except urllib.error.URLError as exc:raise RuntimeError('Canvas bridge unavailable. Run mv.py canvas --project-dir <project> and open ComfyUI. No task submitted.') from exc

if __name__=='__main__':
    import sys
    print(json.dumps(call(sys.argv[1] if len(sys.argv)>1 else 'status',**(json.loads(sys.argv[2]) if len(sys.argv)>2 else {})),ensure_ascii=False,indent=2))
