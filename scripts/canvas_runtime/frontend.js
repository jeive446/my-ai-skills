const comfyOrigin=window.localMvCanvasConfig.comfy_url??window.location.origin;
const {app}=await import(comfyOrigin+'/scripts/app.js');
const {api}=await import(comfyOrigin+'/scripts/api.js');

// Token and websocket URL are supplied by the small on-disk extension loader.
if (!window.localMvCanvas) {
  const config = window.localMvCanvasConfig;
  const bridgeOrigin=config.ws.replace(/^ws/,'http').replace(/\/browser$/,'');
  const mediaResponse=await fetch(bridgeOrigin+'/media_index?token='+encodeURIComponent(config.token));
  if(!mediaResponse.ok)throw new Error('Project media index unavailable');
  let verifiedMedia=(await mediaResponse.json()).verified_input_paths;
  async function refreshMedia(){const r=await fetch(bridgeOrigin+'/media_index?token='+encodeURIComponent(config.token));if(!r.ok)throw new Error('Media index unavailable');verifiedMedia=(await r.json()).verified_input_paths;await app.reloadNodeDefs();}
  const state = {follow: true, busy: false, baseline: null, execution: null, mapping: {}, socket: null};
  state.version='skill-3';window.localMvCanvas = state;
  const panel = document.createElement('div');
  panel.id = 'local-mv-canvas-status';
  Object.assign(panel.style, {position:'fixed',bottom:'12px',left:'90px',zIndex:'10000',background:'#1d2530',color:'#eee',padding:'10px 14px',border:'1px solid #60768a',borderRadius:'8px',font:'13px sans-serif',maxWidth:'540px'});
  const title = document.createElement('strong'); title.textContent = 'MV Agent'; panel.append(title);
  const follow = document.createElement('input'); follow.type='checkbox'; follow.checked=true; follow.style.marginLeft='12px';
  const label = document.createElement('label'); label.append(follow, document.createTextNode(' 跟随 Agent')); panel.append(label);
  const save = document.createElement('button'); save.textContent='保存手动修改'; save.style.marginLeft='12px'; panel.append(save);
  const info = document.createElement('div'); info.textContent='连接本机同步桥…'; info.style.marginTop='6px'; panel.append(info);
  const warn = document.createElement('div'); warn.style.color='#ffcf82'; panel.append(warn);
  const results=document.createElement('div');panel.append(results);
  const canonical = (v) => JSON.stringify(v, function(k,x) { if(x && !Array.isArray(x) && typeof x==='object') return Object.fromEntries(Object.entries(x).sort(([a],[b])=>a.localeCompare(b))); return x; });
  const graphFingerprint = () => {
    const g=app.rootGraph.serialize();
    return canonical({nodes:g.nodes.map(n=>({id:n.id,type:n.type,mode:n.mode,widgets_values:n.widgets_values,inputs:n.inputs,outputs:n.outputs})),links:g.links});
  };
  async function revision(){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(graphFingerprint())))].map(x=>x.toString(16).padStart(2,'0')).join('');}
  function checkEditable(message={}) {
    if(!state.follow)throw new Error('Follow disabled: manual editing is protected.');
    if(state.baseline && state.baseline!==graphFingerprint()) {
      pause('检测到手动修改，Agent 已停止编辑。请先保存修改，再开启跟随。');
      throw new Error('Unsaved manual edits protected; no Agent edit applied.');
    }
  }
  function send(v) { if(state.socket?.readyState===1) state.socket.send(JSON.stringify(v)); }
  function pause(reason) {state.follow=false;follow.checked=false;send({type:'follow',enabled:false});warn.textContent=reason;}
  function highlight(nodeId) {
    for(const node of app.rootGraph.nodes)if(node.__localMvOriginalColor!==undefined){node.color=node.__localMvOriginalColor;delete node.__localMvOriginalColor;}
    const node=nodeId==null?null:app.rootGraph.getNodeById(isNaN(Number(nodeId))?nodeId:Number(nodeId));
    if(node){node.__localMvOriginalColor=node.color??null;node.color='#276349';}
    app.canvas.setDirty(true,true);
  }
  follow.onchange=()=>{state.follow=follow.checked;warn.textContent='';if(state.follow)state.baseline=graphFingerprint();send({type:'follow',enabled:state.follow});};
  save.onclick=()=>{send({type:'save_edit'});info.textContent='正在保存手动修改（不提交生成）';};
  async function exportGraph() {
    const wasClean=!state.baseline || state.baseline===graphFingerprint();
    const result=await app.graphToPrompt();
    if(wasClean)state.baseline=graphFingerprint();
    return {workflow:result.workflow,api:result.output,client_id:api.clientId,warnings:[],revision:await revision()};
  }
  function getNode(id){const node=app.rootGraph.getNodeById(isNaN(Number(id))?id:Number(id));if(!node)throw new Error('Node not found: '+id);return node;}
  function nodeInfo(node){return {id:String(node.id),class_type:node.type,title:node.title,position:[...node.pos],inputs:(node.inputs??[]).map(x=>({name:x.name,type:x.type,link:x.link})),outputs:(node.outputs??[]).map(x=>({name:x.name,type:x.type,links:x.links})),widgets:(node.widgets??[]).filter(w=>w.type!=='button').map(w=>({name:w.name,type:w.type,value:w.value}))};}
  function setWidgets(node,values){
    let pending=Object.entries(values??{});
    for(let attempt=0;attempt<4 && pending.length;attempt++) {
      const next=[];
      for(const [name,value] of pending){
        if(node.inputs?.some(input=>input.name===name && input.link!=null))throw new Error('Widget is connected; disconnect it before setting '+name);
        if(Array.isArray(value) && value.length===2 && typeof value[0]==='string' && app.rootGraph.getNodeById(value[0]))throw new Error('Use canvas_connect for node links');
        const widget=node.widgets?.find(w=>w.name===name);
        if(!widget){next.push([name,value]);continue;}
        const old=widget.value;widget.value=value;widget.callback?.(value);node.onWidgetChanged?.(name,value,old,widget);
      }
      pending=next;
    }
    if(pending.length)throw new Error('Unknown widget names: '+pending.map(x=>x[0]).join(', '));
  }
  async function mutate(message){
    checkEditable(message);
    if(message.expected_revision && message.expected_revision!==await revision())throw new Error('Graph revision changed; inspect canvas_list_nodes before editing.');
    const before=await exportGraph();state.busy=true;
    try{
      let edited=null;
      if(message.cmd==='new_graph'){
        send({type:'backup',...before});await app.loadApiJson({},message.name+'.json');state.execution=null;results.replaceChildren();delete results.dataset.url;
      }else if(message.cmd==='restore_gui'){
        send({type:'backup',...before});await app.loadGraphData(message.workflow);state.execution=null;
      }else if(message.cmd==='add_node'){
        if(message.class_type==='LoadImage')await refreshMedia();
        const node=LiteGraph.createNode(message.class_type);
        if(!node)throw new Error('Unregistered ComfyUI node: '+message.class_type);
        if(message.title)node.title=message.title;
        node.pos=message.position??[80+(app.rootGraph.nodes.length%5)*380,100+Math.floor(app.rootGraph.nodes.length/5)*400];
        app.rootGraph.add(node);setWidgets(node,message.inputs);edited=String(node.id);
      }else if(message.cmd==='set_inputs'){
        const node=getNode(message.node_id);setWidgets(node,message.inputs);edited=String(node.id);
      }else if(message.cmd==='connect_nodes'){
        const source=getNode(message.source_node),target=getNode(message.target_node);
        const out=typeof message.source_output==='number'?message.source_output:source.outputs.findIndex(x=>x.name===message.source_output);
        if(out<0 || !source.outputs[out])throw new Error('Output socket not found');
        let input=target.inputs.findIndex(x=>x.name===message.target_input);
        if(input<0){const w=target.widgets?.find(x=>x.name===message.target_input);if(w && target.convertWidgetToInput?.(w))input=target.inputs.findIndex(x=>x.name===message.target_input);}
        if(input<0)throw new Error('Input socket not found: '+message.target_input);
        if(target.inputs[input].link!=null)throw new Error('Input already connected; disconnect it explicitly first');
        if(!source.connect(out,target,input))throw new Error('Connection refused: incompatible socket types');
        edited=String(target.id);
      }else if(message.cmd==='disconnect_input'){
        const node=getNode(message.node_id),slot=node.inputs.findIndex(x=>x.name===message.input_name);
        if(slot<0)throw new Error('Input socket not found');node.disconnectInput(slot);edited=String(node.id);
      }else if(message.cmd==='remove_node'){
        app.rootGraph.remove(getNode(message.node_id));
      }
      state.execution=null;state.baseline=graphFingerprint();app.canvas.setDirty(true,true);
      // Let each node/edge change paint before acknowledging the MCP edit.
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      info.textContent='Agent 画布编辑：'+message.cmd+(edited?' · 节点 '+edited:'');warn.textContent='';
      const result=await exportGraph();state.baseline=graphFingerprint();
      return {...result,node_id:edited,nodes:app.rootGraph.nodes.map(nodeInfo)};
    }catch(error){await app.loadGraphData(before.workflow);state.baseline=graphFingerprint();throw error;}
    finally{state.busy=false;}
  }
  async function loadApi(message) {
    if(message.observe && !state.follow) throw new Error('Follow disabled; current canvas was not replaced.');
    if(message.preserve!==false && state.baseline && state.baseline!==graphFingerprint()) {
      pause('检测到手动修改，已暂停跟随。请先保存修改，再开启跟随。');
      throw new Error('Unsaved manual edits protected; follow paused.');
    }
    const before=await exportGraph();
    if(message.preserve!==false && before.workflow.nodes?.length) send({type:'backup',...before});
    state.busy=true;
    try {
      await refreshMedia();
      await app.loadApiJson(message.prompt, message.name+'.json');
      // The native importer invokes widget callbacks, including nested dynamic widgets.
      // Restore all named scalar widgets a second time after those callbacks settle.
      for (const [id,spec] of Object.entries(message.prompt)) {
        const node=app.rootGraph.getNodeById(isNaN(Number(id))?id:Number(id));
        if(!node) throw new Error('Native importer failed to create '+id);
        for(const [name,value] of Object.entries(spec.inputs)) {
          if(Array.isArray(value))continue;
          const widget=node.widgets?.find(w=>w.name===name);
          if(widget && canonical(widget.value)!==canonical(value)) {widget.value=value;widget.callback?.(value);}
        }
        for(const widget of node.widgets??[])if(widget.name==='control_after_generate')widget.value='fixed';
      }
      app.rootGraph.arrange();
      app.canvas.fitView?.();
      app.canvas.setDirty(true,true);
      state.mapping={};for(const id of Object.keys(message.prompt))state.mapping[id]=id;
      state.execution=message.execution;
      results.replaceChildren();delete results.dataset.url;
      state.baseline=graphFingerprint();
      info.textContent=message.name+(message.execution?' · '+message.execution.prompt_id.slice(0,8):' · 参数可编辑');
      warn.textContent='';
      const result=await exportGraph();
      state.baseline=graphFingerprint();
      return result;
    } finally {state.busy=false;}
  }
  async function handle(message) {
    if(message.rid) {
      try {
        let result;
        if(message.cmd==='load_api')result=await loadApi(message);
        else if(message.cmd==='export')result=await exportGraph();
        else if(message.cmd==='bind_execution'){
          checkEditable(message);
          if(message.expected_revision && message.expected_revision!==await revision())throw new Error('Graph revision changed before submission');
          state.execution=message.execution;results.replaceChildren();delete results.dataset.url;info.textContent='已准备提交 · '+message.execution.prompt_id.slice(0,8);result={bound:true};
        }
        else if(message.cmd==='check_editable'){checkEditable(message);result={editable:true,revision:await revision()};}
        else if(message.cmd==='list_nodes')result={nodes:app.rootGraph.nodes.map(nodeInfo),revision:await revision()};
        else if(['new_graph','add_node','set_inputs','connect_nodes','disconnect_input','remove_node','restore_gui'].includes(message.cmd))result=await mutate(message);
        else if(message.cmd==='roundtrip') {
          const before=await exportGraph();state.busy=true;
          try{await app.loadGraphData(before.workflow);result=await exportGraph();state.baseline=graphFingerprint();}
          finally{state.busy=false;}
        }
        else throw new Error('Unsupported command');
        send({rid:message.rid,ok:true,result});
      }catch(e){warn.textContent=String(e.message||e);send({rid:message.rid,ok:false,error:String(e.message||e)});}
    } else if(message.event==='follow') {state.follow=message.enabled;follow.checked=state.follow;}
    else if(message.event==='saved_edit'){state.baseline=graphFingerprint();info.textContent='已保存 '+message.saved+' · 未提交生成';}
    else if(message.event==='save_error'){warn.textContent='保存失败：'+message.message;}
    else if(message.event==='execution' && state.execution) {
      const d=message.data;
      if(d.prompt_id && d.prompt_id!==state.execution.prompt_id)return;
      const sourceId=d.node===null?null:String(d.node??'');
      const nodeId=sourceId?.replaceAll(':','_');
      if(message.type==='executing') {
        highlight(nodeId);
        info.textContent=sourceId?('执行节点 '+sourceId+' · '+state.execution.prompt_id.slice(0,8)):'任务完成';
        app.canvas.setDirty(true,true);
      } else if(message.type==='progress') {if(nodeId)highlight(nodeId);info.textContent='采样 '+d.value+'/'+d.max+' · '+Math.round(100*d.value/d.max)+'%';}
      else if(message.type==='execution_error') {warn.textContent='生成错误：'+(d.exception_message||'请查看任务详情');}
      else if(message.type==='executed') {
        if(!d.prompt_id)return;
        info.textContent='节点 '+sourceId+' 已输出';
        for(const item of [...(d.output?.images??[]),...(d.output?.videos??[]),...(d.output?.gifs??[])]) {
          if(!/\.(mp4|webm|mov)$/i.test(item.filename))continue;
          const url=comfyOrigin+'/view?'+new URLSearchParams({filename:item.filename,subfolder:item.subfolder??'',type:item.type??'output'});
          if(results.dataset.url===url)continue;
          results.replaceChildren();results.dataset.url=url;
          const caption=document.createElement('div');caption.textContent='结果：'+item.filename;results.append(caption);
          const video=document.createElement('video');video.src=url;video.controls=true;video.preload='metadata';video.style.cssText='width:320px;max-height:190px;margin-top:8px';results.append(video);
        }
      }
      else if(message.type==='execution_success') {highlight(null);info.textContent='生成完成 · '+state.execution.prompt_id.slice(0,8);}
      send({type:'execution_ack',event:message.type,prompt_id:state.execution.prompt_id,node:sourceId,display:info.textContent});
    }
  }
  function connect() {
    const socket=new WebSocket(config.ws+'?token='+encodeURIComponent(config.token));state.socket=socket;
    socket.onopen=()=>{info.textContent='已连接 · 等待 Agent 或手动打开工作流';send({type:'hello',client_id:api.clientId});};
    socket.onmessage=e=>{handle(JSON.parse(e.data)).catch(err=>warn.textContent=String(err));};
    socket.onclose=()=>{if(state.disposed)return;info.textContent='同步桥未连接 · 3秒后重连';setTimeout(connect,3000);};
    socket.onerror=()=>{info.textContent='同步桥连接失败';};
  }
  let initialized=false;
  async function initialize(){if(initialized)return;initialized=true;document.body.append(panel);await app.reloadNodeDefs();state.baseline=graphFingerprint();connect();}
  for(const type of ['execution_start','executing','progress','executed','execution_success','execution_error','execution_cached']) {
    api.addEventListener(type,event=>{if(state.disposed)return;handle({event:'execution',type,data:event.detail}).catch(err=>warn.textContent=String(err));});
  }
  app.registerExtension({name:'LocalMV.Canvas.Skill3',afterLoadGraph(){if(!state.busy)state.baseline=graphFingerprint();},beforeRegisterNodeDef(nodeType,nodeData){
    if(nodeData.name==='LoadImage' && Array.isArray(nodeData.input?.required?.image?.[0])) {
      nodeData.input.required.image[0]=[...new Set([...nodeData.input.required.image[0],...verifiedMedia])];
    }
  },nodeCreated(node){
    if(node.type==='LoadImage') {
      const widget=node.widgets?.find(w=>w.name==='image');
      if(widget && Array.isArray(widget.options?.values))widget.options.values=[...new Set([...widget.options.values,...verifiedMedia])];
    }
    for(const widget of node.widgets??[])if(widget.name==='control_after_generate')widget.value='fixed';
  },async setup(){initialize();}});
  if(app.rootGraph && app.canvas)initialize();
}
