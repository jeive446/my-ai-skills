"""Project-scoped loopback bridge. Never starts, stops, or interrupts ComfyUI."""
import asyncio
import copy
import hashlib
import json
import math
import os
import secrets
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

HERE = Path(__file__).resolve().parent
from .runtime_config import STATE, SETTINGS, settings, configure_paths
CONFIG=SETTINGS
config=settings()
PROJECT, OUTPUT, USER_WORKFLOWS=configure_paths(config)
COMFY=config['comfy_url'].rstrip('/')
PORT=config['port']
TOKEN=config['token']
ALLOWED_ORIGINS={COMFY,COMFY.replace('127.0.0.1','localhost')}
TRACKED_PREFIXES=config.get('tracked_prefixes',[])


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.partial')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def normalize_prompt(prompt):
    """Preserve API ids except colons: matches ComfyUI's native API importer."""
    taken = {k for k in prompt if ':' not in k}
    mapping = {}
    for key in prompt:
        target = key.replace(':', '_')
        while target in taken and target != key:
            target += '_'
        mapping[key] = target
        taken.add(target)
    result = copy.deepcopy(prompt)
    normalized = {}
    for key, node in result.items():
        for name, value in node.get('inputs', {}).items():
            if isinstance(value, list) and len(value) == 2 and str(value[0]) in mapping:
                node['inputs'][name] = [mapping[str(value[0])], value[1]]
        normalized[mapping[key]] = node
    return normalized, mapping


def compare(expected, actual):
    differences = []
    for key, spec in expected.items():
        got = actual.get(str(key))
        if got is None:
            differences.append({'node': key, 'error': 'missing_node'})
            continue
        if got['class_type'] != spec['class_type']:
            differences.append({'node': key, 'error': 'class_type', 'expected': spec['class_type'], 'actual': got['class_type']})
        for name, value in spec.get('inputs', {}).items():
            other = got.get('inputs', {}).get(name, '__MISSING__')
            if isinstance(value, list) and isinstance(other, list) and len(value) == len(other) == 2:
                same = str(value[0]) == str(other[0]) and value[1] == other[1]
            else:
                same = value == other
                if isinstance(value, (int,float)) and not isinstance(value,bool) and isinstance(other,(int,float)) and not isinstance(other,bool):
                    same = math.isclose(value, other, rel_tol=1e-12, abs_tol=1e-12)
            if not same:
                differences.append({'node': key, 'input': name, 'expected': value, 'actual': other})
    return differences


class Bridge:
    def __init__(self):
        self.sockets = {}
        self.pending = {}
        self.follow = True
        self.session = None
        self.current = None
        self.observed = set()
        self.latest_event = None
        self.last_error = None
        self.prepared = {}
        self.event_count = 0
        self.last_browser_ack = None
        self.completed = set()
        self.prepare_lock = asyncio.Lock()
        self.editing = False

    async def browser_command(self, cmd, **args):
        candidates = [s for s in self.sockets.values() if not s.closed]
        if not candidates:
            raise RuntimeError('No browser bridge connected. Reload the ComfyUI page.')
        if len(candidates) != 1:
            raise RuntimeError('More than one bridge tab connected; close extra ComfyUI bridge tabs before changing the graph.')
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await candidates[0].send_json({'rid': rid, 'cmd': cmd, **args})
            return await asyncio.wait_for(future, timeout=120)
        finally:
            self.pending.pop(rid, None)

    async def broadcast(self, data):
        for sock in list(self.sockets.values()):
            if not sock.closed:
                await sock.send_json(data)

    async def persist(self, name, source, result, mapping=None, execution=None):
        safe = ''.join(c for c in name if c.isalnum() or c in '-_')[:120] or 'workflow'
        exported = result['api']
        expected, default_mapping = normalize_prompt(source)
        differences = compare(expected, exported)
        report = {'name': name, 'time': time.time(), 'source_nodes': len(source), 'exported_nodes': len(exported),
                  'differences': differences, 'passed': not differences, 'node_id_mapping': mapping or default_mapping,
                  'browser_warnings': result.get('warnings', []), 'execution': execution,
                  'source_sha256': hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()}
        write(OUTPUT / (safe + '.source.api.json'), source)
        write(OUTPUT / (safe + '.api.json'), exported)
        write(OUTPUT / (safe + '.workflow.json'), result['workflow'])
        write(OUTPUT / (safe + '.verification.json'), report)
        if report['passed']:
            write(USER_WORKFLOWS / (safe + '.json'), result['workflow'])
        return report

    async def prepare(self, source, name, execution=None, observe=False, preserve=True):
        async with self.prepare_lock:
            return await self._prepare(source,name,execution,observe,preserve)

    async def _prepare(self, source, name, execution=None, observe=False, preserve=True):
        normalized, mapping = normalize_prompt(source)
        result = await self.browser_command('load_api', prompt=normalized, name=name, execution=execution,
                                            observe=observe, preserve=preserve)
        report = await self.persist(name, source, result, mapping, execution)
        if not report['passed']:
            raise RuntimeError('Native import/export mismatch: ' + json.dumps(report['differences'][:8], ensure_ascii=False))
        self.current = {'name': name, 'execution': execution, 'mapping': mapping, 'source': source,
                        'workflow': result['workflow'], 'api': normalized}
        self.last_error = None
        self.editing = False
        self.prepared[report['source_sha256']] = self.current
        return {'report': report, 'workflow': result['workflow'], 'api': normalized, 'client_id': result.get('client_id')}

    async def watch(self):
        while True:
            try:
                async with self.session.ws_connect(COMFY.replace('http', 'ws') + '/ws?clientId=local-mv-observer-' + uuid.uuid4().hex) as ws:
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        event = json.loads(message.data)
                        kind, data = event.get('type'), event.get('data', {})
                        self.event_count += 1
                        if kind in ('execution_start', 'executing', 'progress', 'executed', 'execution_success', 'execution_error', 'execution_cached'):
                            self.latest_event = {'type': kind, 'data': data, 'at': time.time()}
                            with (STATE/'execution_events.jsonl').open('a',encoding='utf-8') as log:
                                log.write(json.dumps(self.latest_event,ensure_ascii=False)+'\n')
                            await self.broadcast({'event': 'execution', 'type': kind, 'data': data})
            except Exception as exc:
                self.last_error = 'Event observer: ' + str(exc)
                await asyncio.sleep(3)

    async def poll(self):
        while True:
            try:
                async with self.session.get(COMFY + '/queue') as r:
                    queue = await r.json()
                for item in queue.get('queue_running', []):
                    prompt_id, prompt = item[1], item[2]
                    prefixes = [str(n.get('inputs', {}).get('filename_prefix', '')) for n in prompt.values()]
                    ours = any(x.startswith(prefix) for x in prefixes for prefix in TRACKED_PREFIXES) or (item[3].get('local_mv_canvas',{}).get('project_root')==str(PROJECT))
                    if ours and self.follow and not self.editing and self.sockets and prompt_id not in self.observed:
                        self.observed.add(prompt_id)
                        name = next((p.split('/')[-1] for p in prefixes if p and not p.endswith('_latent')), 'current')
                        try:
                            await self.prepare(prompt, 'Current_' + name + '_' + prompt_id[:8],
                                               execution={'prompt_id': prompt_id, 'attached_after_submission': True}, observe=True)
                        except Exception as exc:
                            self.last_error = 'Follow: ' + str(exc)
                            # Never retry mutations automatically after a lost response.
                active_id=(self.current or {}).get('execution') or {}
                active_id=active_id.get('prompt_id')
                if active_id and active_id not in self.completed and not any(x[1]==active_id for x in queue.get('queue_running',[])):
                    async with self.session.get(COMFY+'/history/'+active_id) as r:
                        history=await r.json()
                    item=history.get(active_id)
                    if item and item.get('status',{}).get('completed'):
                        self.completed.add(active_id)
                        for node,output in item.get('outputs',{}).items():
                            await self.broadcast({'event':'execution','type':'executed','data':{'prompt_id':active_id,'node':node,'output':output}})
                        await self.broadcast({'event':'execution','type':'execution_success','data':{'prompt_id':active_id}})
                await asyncio.sleep(1)
            except Exception as exc:
                self.last_error = 'Queue observer: ' + str(exc)
                await asyncio.sleep(3)

    async def operation(self, action, data):
        global PROJECT,OUTPUT,USER_WORKFLOWS,TRACKED_PREFIXES,config
        if action == 'status':
            return {'project_root':str(PROJECT),'runtime':'skill-1','editing':self.editing,'browser_tabs': len(self.sockets), 'follow': self.follow, 'current': {k:v for k,v in (self.current or {}).items() if k in ('name','execution','mapping')},
                    'last_error': self.last_error, 'latest_event': self.latest_event, 'event_count': self.event_count,'last_browser_ack':self.last_browser_ack}
        if action=='select_project':
            root=Path(data['project_dir']).resolve()
            if not root.is_dir():raise ValueError('Project directory must already exist')
            async with self.prepare_lock:
                if root!=PROJECT:
                    async with self.session.get(COMFY+'/queue') as r:q=await r.json()
                    for item in q.get('queue_running',[])+q.get('queue_pending',[]):
                        if any(str(n.get('inputs',{}).get('filename_prefix','')).startswith(prefix) for n in item[2].values() for prefix in TRACKED_PREFIXES):
                            raise RuntimeError('Current project still has queued/running work; finish it before selecting a different project.')
                    if self.sockets:
                        await self.browser_command('check_editable')
                        before=await self.browser_command('export')
                        write(OUTPUT/'backups'/('ProjectSwitch_'+uuid.uuid4().hex+'.workflow.json'),before['workflow'])
                    config['project_root']=str(root);config['tracked_prefixes']=[]
                    PROJECT,OUTPUT,USER_WORKFLOWS=configure_paths(config)
                    self.current=None;self.observed.clear();self.prepared.clear()
                project_id=data.get('project_id')
                if project_id:
                    if '/' in project_id or '\\' in project_id:raise ValueError('project_id must be one name')
                    prefix='video/mv/'+project_id+'/'
                    if prefix not in config['tracked_prefixes']:config['tracked_prefixes'].append(prefix)
                TRACKED_PREFIXES=config['tracked_prefixes']
                write(SETTINGS,config)
            return {'project_root':str(PROJECT),'workflows':str(USER_WORKFLOWS),'tracked_prefixes':TRACKED_PREFIXES}
        if action == 'follow':
            self.follow = bool(data['enabled'])
            if self.follow:self.editing=False
            if self.follow:self.observed.clear()
            await self.broadcast({'event': 'follow', 'enabled': self.follow})
            return {'follow': self.follow}
        if action == 'prepare':
            return await self.prepare(data['prompt'], data.get('name', 'Current_Agent'), execution=data.get('execution'),
                                      observe=data.get('observe', False), preserve=data.get('preserve', True))
        if action == 'export':
            return await self.browser_command('export')
        if action in ('new_graph','list_nodes','add_node','set_inputs','connect_nodes','disconnect_input','remove_node','restore_gui'):
            async with self.prepare_lock:
                result=await self.browser_command(action,**data)
                if action!='list_nodes':
                    self.editing=True
                    write(OUTPUT/'edits'/(time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+'.json'),{'action':action,'arguments':data,'result':result,'created':time.time()})
                return result
        if action == 'roundtrip':
            before=await self.browser_command('export')
            after=await self.browser_command('roundtrip')
            differences=compare(before['api'],after['api'])
            report={'passed':not differences,'differences':differences,'gpu_jobs_submitted':0,'kind':'GUI serialize -> loadGraphData -> export API'}
            write(OUTPUT/'gui_reload_verification.json',report)
            return report
        if action == 'save_edit':
            result = await self.browser_command('export')
            name = 'Manual_' + time.strftime('%Y%m%d_%H%M%S')
            write(OUTPUT / 'manual' / (name + '.workflow.json'), result['workflow'])
            write(OUTPUT / 'manual' / (name + '.api.json'), result['api'])
            write(USER_WORKFLOWS / 'manual' / (name + '.json'), result['workflow'])
            return {'saved': name, 'submitted': False}
        if action == 'submit_current':
            result = await self.browser_command('export')
            async with self.session.get(COMFY + '/queue') as r:
                queue = await r.json()
            if queue.get('queue_running') or queue.get('queue_pending'):
                raise RuntimeError('ComfyUI busy; no task submitted.')
            prompt_id = data.get('prompt_id') or str(uuid.uuid4())
            snapshot = {'api': result['api'], 'workflow': result['workflow'], 'prompt_id': prompt_id,
                        'name': data.get('name', 'manual'), 'created': time.time()}
            write(OUTPUT / 'submissions' / (prompt_id + '.json'), snapshot)
            await self.browser_command('bind_execution',execution={'prompt_id':prompt_id,'attached_after_submission':False},expected_revision=result['revision'])
            payload = {'prompt': result['api'], 'prompt_id': prompt_id, 'client_id': result.get('client_id'),
                       'extra_data': {'extra_pnginfo': {'workflow': result['workflow']},'local_mv_canvas':{'project_root':str(PROJECT)}}}
            async with self.session.post(COMFY + '/prompt', json=payload) as r:
                response = await r.json()
                if r.status != 200 or 'error' in response:
                    raise RuntimeError(json.dumps(response, ensure_ascii=False))
            self.current={'name':snapshot['name'],'execution':{'prompt_id':prompt_id,'attached_after_submission':False},'mapping':{k:k for k in result['api']},'source':result['api'],'api':result['api'],'workflow':result['workflow']}
            self.observed.add(prompt_id);self.editing=False
            return response
        raise ValueError('Unknown action: ' + action)


bridge = Bridge()


@web.middleware
async def guard(request, handler):
    origin = request.headers.get('Origin')
    if origin and origin not in ALLOWED_ORIGINS:
        return web.Response(status=403)
    if request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
        return web.Response(status=403)
    if request.method == 'OPTIONS':
        response = web.Response(status=204)
    else:
        response = await handler(request)
    if origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
    if request.path=='/frontend.js':response.headers['Cache-Control']='no-store'
    return response


async def websocket(request):
    if not secrets.compare_digest(request.query.get('token', ''), TOKEN):
        return web.Response(status=403)
    ws = web.WebSocketResponse(max_msg_size=20*1024*1024, heartbeat=25)
    await ws.prepare(request)
    tab = uuid.uuid4().hex
    bridge.sockets[tab] = ws
    if len(bridge.sockets)==1:bridge.observed.clear()
    await ws.send_json({'event': 'follow', 'enabled': bridge.follow})
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get('rid') in bridge.pending:
                future = bridge.pending[data['rid']]
                if not future.done():
                    if data.get('ok'):
                        future.set_result(data['result'])
                    else:
                        future.set_exception(RuntimeError(data.get('error', 'Browser command failed')))
            elif data.get('type') == 'follow':
                bridge.follow = bool(data['enabled'])
                if bridge.follow:bridge.observed.clear()
            elif data.get('type') == 'save_edit':
                async def save_and_notify():
                    try:
                        result=await bridge.operation('save_edit', {})
                        await ws.send_json({'event':'saved_edit',**result})
                    except Exception as exc:
                        await ws.send_json({'event':'save_error','message':str(exc)})
                asyncio.create_task(save_and_notify())
            elif data.get('type') == 'execution_ack':
                bridge.last_browser_ack=data
                with (STATE/'browser_events.jsonl').open('a',encoding='utf-8') as log:
                    log.write(json.dumps({**data,'at':time.time()},ensure_ascii=False)+'\n')
            elif data.get('type') == 'backup':
                name = 'Before_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
                write(OUTPUT / 'backups' / (name + '.workflow.json'), data['workflow'])
                write(OUTPUT / 'backups' / (name + '.api.json'), data['api'])
    finally:
        bridge.sockets.pop(tab, None)
    return ws


async def rpc(request):
    if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + TOKEN):
        return web.json_response({'error': 'Unauthorized'}, status=403)
    try:
        data = await request.json()
        result = await bridge.operation(data.pop('action'), data)
        return web.json_response({'result': result})
    except Exception as exc:
        bridge.last_error = str(exc)
        return web.json_response({'error': str(exc)}, status=400)


async def media_index(request):
    if not secrets.compare_digest(request.query.get('token',''), TOKEN):
        return web.Response(status=403)
    root=Path(config['comfy_root'])/'input'
    folder=root/'wan22_mv'
    names=[p.relative_to(root).as_posix() for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in ('.png','.jpg','.jpeg','.webp')]
    return web.json_response({'verified_input_paths':names})


async def startup(app):
    bridge.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    app['watch'] = asyncio.create_task(bridge.watch())
    app['poll'] = asyncio.create_task(bridge.poll())


async def shutdown(app):
    app['watch'].cancel(); app['poll'].cancel()
    for sock in list(bridge.sockets.values()):
        await sock.close()
    await bridge.session.close()


if __name__ == '__main__':
    app = web.Application(middlewares=[guard], client_max_size=20*1024*1024)
    app.router.add_get('/browser', websocket)
    app.router.add_get('/health', lambda r: web.json_response({'service': 'local-mv-canvas-bridge', 'port': PORT, 'browsers': len(bridge.sockets)}))
    app.router.add_post('/rpc', rpc)
    app.router.add_get('/media_index', media_index)
    app.router.add_get('/frontend.js', lambda r: web.FileResponse(HERE/'frontend.js'))
    app.on_startup.append(startup); app.on_cleanup.append(shutdown)
    web.run_app(app, host='127.0.0.1', port=PORT, print=None)
