"""Reusable canvas editing tools: every edit is applied in the user's live ComfyUI."""
import asyncio
import json
from pathlib import Path
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from .client import call
from .runtime_setup import ensure_running

mcp=MCPServer('local-mv-canvas')
ensure_running()

async def rpc(action,**kwargs):
    try:return await asyncio.to_thread(call,action,**kwargs)
    except RuntimeError as exc:raise ToolError(str(exc)) from exc

@mcp.tool()
async def canvas_status()->dict:
    """Read selected project, browser connection, current task and real progress. No GPU work."""
    return await rpc('status')

@mcp.tool()
async def canvas_select_project(project_dir:str,project_id:str|None=None)->dict:
    """Select an existing MV directory and optionally register a manifest project id. Refuses switching away from queued/running work or unpreserved edits."""
    return await rpc('select_project',project_dir=project_dir,project_id=project_id)

@mcp.tool()
async def canvas_follow(enabled:bool)->dict:
    """Toggle live following. Disabled follow protects manual editing from Agent mutations."""
    return await rpc('follow',enabled=enabled)

@mcp.tool()
async def canvas_export()->dict:
    """Read current GUI, executable API, browser client id and revision. Does not generate."""
    return await rpc('export')

@mcp.tool()
async def canvas_open_api(path:str,name:str='Current_Agent')->dict:
    """Native-import a JSON within selected project and compare every source API input. For starting a template; use node tools for visible incremental edits."""
    status=await rpc('status');file=Path(path).resolve()
    if not file.is_relative_to(Path(status['project_root']).resolve()):raise ValueError('File is outside selected project')
    result=await rpc('prepare',prompt=json.loads(file.read_text(encoding='utf-8-sig')),name=name)
    return result['report']

@mcp.tool()
async def canvas_new_graph(name:str,expected_revision:str|None=None)->dict:
    """Start a new visible graph, backing up current GUI first. Never queues a render."""
    return await rpc('new_graph',name=name,expected_revision=expected_revision)

@mcp.tool()
async def canvas_list_nodes()->dict:
    """Read live node ids, types, widget values, sockets and graph revision before editing."""
    return await rpc('list_nodes')

@mcp.tool()
async def canvas_add_node(class_type:str,title:str|None=None,inputs:dict|None=None,position:list[float]|None=None,expected_revision:str|None=None)->dict:
    """Create ONE real visible ComfyUI node by registered class type; optionally set named widgets. Returns its actual id and new revision. No GPU work."""
    return await rpc('add_node',class_type=class_type,title=title,inputs=inputs or {},position=position,expected_revision=expected_revision)

@mcp.tool()
async def canvas_set_inputs(node_id:str,inputs:dict,expected_revision:str|None=None)->dict:
    """Change named widget parameters on an existing visible node. Does not rebuild the graph or change submitted tasks."""
    return await rpc('set_inputs',node_id=node_id,inputs=inputs,expected_revision=expected_revision)

@mcp.tool()
async def canvas_connect(source_node:str,source_output:str|int,target_node:str,target_input:str,expected_revision:str|None=None)->dict:
    """Connect one real output socket to one target input; converts a widget to an input if supported. Refuses implicit replacement of an occupied input."""
    return await rpc('connect_nodes',source_node=source_node,source_output=source_output,target_node=target_node,target_input=target_input,expected_revision=expected_revision)

@mcp.tool()
async def canvas_disconnect(node_id:str,input_name:str,expected_revision:str|None=None)->dict:
    """Disconnect one named input on the live graph. No GPU work."""
    return await rpc('disconnect_input',node_id=node_id,input_name=input_name,expected_revision=expected_revision)

@mcp.tool()
async def canvas_remove_node(node_id:str,expected_revision:str|None=None)->dict:
    """Remove one visible node and its links; edit journal retains preceding operations. Does not delete any media."""
    return await rpc('remove_node',node_id=node_id,expected_revision=expected_revision)

@mcp.tool()
async def canvas_save_edits()->dict:
    """Save actual edited GUI/API as a new revision in selected project and ComfyUI list. Never submits GPU work."""
    return await rpc('save_edit')

@mcp.tool()
async def canvas_restore_snapshot(path:str,expected_revision:str|None=None)->dict:
    """Restore a saved GUI JSON within selected project, preserving current GUI first. No GPU work; refuses conflicting manual edits."""
    status=await rpc('status');file=Path(path).resolve()
    if not file.is_relative_to(Path(status['project_root']).resolve()):raise ValueError('Snapshot is outside selected project')
    saved=json.loads(file.read_text(encoding='utf-8-sig'))
    return await rpc('restore_gui',workflow=saved.get('workflow',saved),expected_revision=expected_revision)

@mcp.tool()
async def canvas_submit_current(name:str)->dict:
    """Submit the visible graph ONCE for an already authorized generation. Saves exact GUI/API first, refuses occupied queue, never auto-retries. Not for connection tests."""
    return await rpc('submit_current',name=name)
