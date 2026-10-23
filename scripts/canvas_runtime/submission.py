"""Default visible submission for every project using this skill."""
import json
import os
import time
import uuid
from pathlib import Path
from .client import call
from .runtime_setup import ensure_running
from .runtime_config import settings

def bind_project(path):
    path=Path(path).resolve()
    active=settings().get('project_root')
    root=Path(active).resolve() if active and path.is_relative_to(Path(active).resolve()) else path
    os.environ['MV_PROJECT_ROOT']=str(root)
    return root

def queue(prompt,api_json,prompt_id=None):
    ensure_running()
    root=os.environ.get('MV_PROJECT_ROOT')
    if not root:raise RuntimeError('MV_PROJECT_ROOT not bound. Use mv.py generate --manifest ...; no GPU task submitted.')
    prefixes=[n.get('inputs',{}).get('filename_prefix','') for n in prompt.values()]
    prefix=next((x for x in prefixes if x and not x.endswith('_latent')),'Current_Agent')
    parts=prefix.split('/')
    project_id=parts[2] if len(parts)>3 and parts[:2]==['video','mv'] else None
    call('select_project',project_dir=root,project_id=project_id)
    status=call('status')
    if not status['follow']:raise RuntimeError('Follow paused: save manual edits and enable 跟随 Agent. No GPU task submitted.')
    pid=prompt_id or str(uuid.uuid4())
    prepared=call('prepare',prompt=prompt,name='Current_'+parts[-1]+'_'+pid[:8],observe=True,
                  execution={'prompt_id':pid,'attached_after_submission':False})
    output=Path(root)/'workflows_synced/submissions';output.mkdir(parents=True,exist_ok=True)
    snapshot={'prompt_id':pid,'created':time.time(),'api':prompt,'workflow':prepared['workflow'],'verification':prepared['report']}
    (output/(pid+'.json')).write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    q=api_json('queue')
    if q.get('queue_running') or q.get('queue_pending'):raise RuntimeError('ComfyUI busy; no GPU task submitted.')
    result=api_json('prompt',{'prompt':prompt,'prompt_id':pid,'client_id':prepared.get('client_id'),
         'extra_data':{'extra_pnginfo':{'workflow':prepared['workflow']},'local_mv_canvas':{'project_root':root,'node_id_mapping':prepared['report']['node_id_mapping']}}})
    if result.get('error') or result.get('prompt_id')!=pid:raise RuntimeError('Submission unconfirmed; inspect original prompt_id before retry: '+pid)
    return result
