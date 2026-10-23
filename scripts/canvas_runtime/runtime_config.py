import hashlib
import json
import os
import secrets
from pathlib import Path

STATE=Path(os.environ.get('MV_CANVAS_STATE',str(Path.home()/'.codex/integrations/local-mv-canvas')))
SETTINGS=STATE/'settings.json'

def settings():
    if not SETTINGS.exists():
        STATE.mkdir(parents=True,exist_ok=True)
        SETTINGS.write_text(json.dumps({'token':secrets.token_urlsafe(32),'port':8190,'comfy_url':os.environ.get('WAN_COMFY_URL','http://127.0.0.1:8188'),
          'comfy_root':os.environ.get('WAN_COMFY_ROOT','D:/AI_Video/ComfyUI'),'project_root':None,'tracked_prefixes':[]},indent=2),encoding='utf-8')
    return json.loads(SETTINGS.read_text(encoding='utf-8'))

def configure_paths(config):
    root=Path(config['project_root']).resolve() if config.get('project_root') else STATE/'unselected'
    name=''.join(c for c in root.name if c.isalnum() or c in '-_')[:36] or 'MV'
    namespace='LocalMV_'+name+'_'+hashlib.sha256(str(root).casefold().encode()).hexdigest()[:8]
    output=root/'workflows_synced'
    user=Path(config['comfy_root'])/'user/default/workflows'/namespace
    output.mkdir(parents=True,exist_ok=True);user.mkdir(parents=True,exist_ok=True)
    return root,output,user
