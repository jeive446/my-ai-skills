"""Install/start this skill's browser bridge. Does not restart ComfyUI or download models."""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from .runtime_config import STATE,settings

HERE=Path(__file__).resolve().parent

def ensure_running():
    config=settings()
    def alive():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{config['port']}/health",timeout=2) as r:
                data=json.load(r)
            if data.get('service')!='local-mv-canvas-bridge':raise RuntimeError('Bridge port is used by a different service; migrate it before starting.')
            return True
        except urllib.error.URLError:return False
    if alive():return
    python=Path.home()/'.codex/integrations/comfy-mcp/.venv/Scripts/python.exe'
    if not python.exists():raise RuntimeError('Canvas MCP runtime missing. Install mcp>=2,<3 and aiohttp in an isolated environment; do not alter ComfyUI GPU dependencies.')
    with (STATE/'stdout.log').open('a') as out,(STATE/'stderr.log').open('a') as err:
        subprocess.Popen([str(python),'-m','canvas_runtime.bridge_server'],cwd=str(HERE.parent),stdout=out,stderr=err,
                         creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    for _ in range(30):
        if alive():return
        time.sleep(.15)
    raise RuntimeError('Canvas bridge did not start; inspect '+str(STATE/'stderr.log'))

def install_loader():
    config=settings()
    # This installed extension directory is already served by the live ComfyUI.
    # A future environment may supply its own already-served frontend directory.
    webdir=Path(os.environ.get('MV_CANVAS_WEB_DIR',str(Path(config['comfy_root'])/'custom_nodes/ComfyUI-KJNodes/web')))
    if not webdir.is_dir():raise RuntimeError('No already-served extension directory. Set MV_CANVAS_WEB_DIR to an installed ComfyUI extension web folder.')
    loader=webdir/'local_mv_canvas.js'
    value={'ws':f"ws://127.0.0.1:{config['port']}/browser",'token':config['token'],'comfy_url':config['comfy_url']}
    text='window.localMvCanvasConfig='+json.dumps(value)+';\n'+f"await import('http://127.0.0.1:{config['port']}/frontend.js?v=skill3');\n"
    if not loader.exists() or loader.read_text(encoding='utf-8')!=text:loader.write_text(text,encoding='utf-8')
    return loader

def setup(project_dir,project_id=None):
    install_loader();ensure_running()
    from .client import call
    result=call('select_project',project_dir=str(Path(project_dir).resolve()),project_id=project_id)
    return {'selected':result,'comfy_url':settings()['comfy_url'],'instruction':'Open or refresh ComfyUI; keep one bridge page connected.'}
