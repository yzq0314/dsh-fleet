import {readFile,mkdir} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {modelService,writePrivate} from './model-service.mjs';
import {fleetSessionId} from './handoff.mjs';

export const name = 'dsh-fleet-desktop';
export const inject = ['commands','sessionController','workingDirectory','llm','agentDefaultModel','tools'];
const {version} = JSON.parse(await readFile(new URL('./package.json',import.meta.url),'utf8'));
const idOf = agent => String(agent.session.id);
const stableId = (...parts) => createHash('sha256').update(JSON.stringify(parts)).digest('hex');
const redact = v => String(v??'').replace(/\bBearer\s+[\w./+=-]+/gi,'Bearer [redacted]')
  .replace(/((?:token|api[_-]?key|password|secret|authorization)\s*[:=]\s*)[^\s,;]+/gi,'$1[redacted]')
  .replace(/("(?:[^"\\]*[_-])?(?:token|api[_-]?key|password|secret|authorization|cookie)"\s*:\s*)"(?:\\.|[^"\\])*"/gi,'$1"[redacted]"');
const secretField = /token|api[_-]?key|password|secret|authorization|cookie|private[_-]?key|credential/i;
function summarizeCall(name,input) {
  let args;
  try {args=typeof input==='string'?JSON.parse(input):input;} catch { /* Keep the approval local. */ }
  if (!args || typeof args!=='object' || Array.isArray(args)) return {display:String(name),complete:false};
  let masked=false;
  const plain=JSON.stringify(args,(key,value)=>{
    if(secretField.test(key)){masked=true;return '[redacted]';}
    if(typeof value!=='string')return value;
    const clean=redact(value);
    if(clean!==value)masked=true;
    return clean;
  });
  const display=redact(name)+' '+plain;
  return {display,complete:!masked && redact(name)===name && Buffer.byteLength(display)<eventLimit};
}
const safeText = v => {const s=redact(v);return s.length>24000?s.slice(0,24000)+'…（已截断）':s;};
const eventLimit = 60*1024;
const bytesOf = value => Buffer.byteLength(JSON.stringify(value),'utf8');
function fitEvent(event) {
  if(bytesOf(event)<=eventLimit) return event;
  if(event.type==='approval_required') return null;
  const field=['text','command','name','reason'].sort((a,b)=>
    String(event.data[b]??'').length-String(event.data[a]??'').length)[0];
  if(typeof event.data[field]!=='string') return null;
  const original=event.data[field], suffix='…（已截断）';
  let low=0, high=original.length;
  const data={...event.data,truncated:true}, reduced={...event,data};
  while(low<high) {
    const mid=Math.ceil((low+high)/2);
    data[field]=original.slice(0,mid)+suffix;
    if(bytesOf(reduced)<=eventLimit) low=mid;else high=mid-1;
  }
  data[field]=original.slice(0,low).replace(/[\uD800-\uDBFF]$/,'')+suffix;
  return bytesOf(reduced)<=eventLimit?reduced:null;
}

export async function apply(ctx) {
  const directory = join(process.env.DSH_HOME || join(homedir(),'.dsh'),'fleet-desktop');
  await mkdir(directory,{recursive:true,mode:0o700});
  const startup = stage => writePrivate(join(directory,'plugin-startup.json'),
    {stage,time:new Date().toISOString()}).catch(() => {});
  await startup('reading_pairing');
  const cfg = JSON.parse(await readFile(join(directory,'config.json'),'utf8'));
  const base = new URL(cfg.hub_url);
  if (!['http:','https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw new Error('Invalid private Fleet Hub address');
  if (!cfg.node_id || !/^[a-f0-9]{64}$/.test(cfg.token)) throw new Error('Missing scoped Desktop configuration');
  await startup('loading_sync_state');
  const prefix = base.href.replace(/\/$/,'')+'/api/desktop/'+encodeURIComponent(cfg.node_id);
  const abort = new AbortController();
  const chosen = new Set(cfg.selected_sessions || []);
  let saved;
  const identity=stableId(base.href.replace(/\/$/,''),cfg.node_id,cfg.token);
  // A different Hub, pairing secret or node always gets a separate queue and session enrollment.
  const stateFile = join(directory,'sync-state-'+identity+'.json');
  try { saved = JSON.parse(await readFile(stateFile,'utf8')); }
  catch(e) { if (e.code !== 'ENOENT') throw e; }
  if(saved!==undefined && saved?.identity!==identity)
    throw new Error('Desktop state identity mismatch; leave this file for manual inspection');
  saved??={};
  for (const sid of saved.selected ?? []) chosen.add(sid);
  const outbox = saved.outbox ?? [];
  const muted = new Set(saved.muted ?? []);
  const quarantined=saved.quarantined??[];
  const pending = new Map();
  const commands = new Map();
  let persist = Promise.resolve(), syncing;
  const state = () => {
    const value = {identity,selected:[...chosen],muted:[...muted],outbox:[...outbox],quarantined:[...quarantined]};
    persist = persist.catch(() => {}).then(() => writePrivate(stateFile,value));
    return persist;
  };
  async function hub(path,data) {
    const r = await fetch(prefix+path,{method:data === undefined?'GET':'POST',redirect:'error',
      signal:AbortSignal.any([abort.signal,AbortSignal.timeout(5000)]),
      headers:{Authorization:'Bearer '+cfg.token,'Content-Type':'application/json'},
      body:data === undefined?undefined:JSON.stringify(data)});
    if (!r.ok) {
      const error=new Error('Fleet Hub returned HTTP '+r.status);error.status=r.status;throw error;
    }
    return r.json();
  }
  function flush() {
    if (syncing) return syncing;
    syncing = (async () => {
      while(outbox.length && !abort.signal.aborted) {
        const head = outbox[0];
        let response;
        try {response=await hub('/events',head);}
        catch(error) {
          if(![400,405,409,413,415,422].includes(error.status)) throw error;
          quarantined.push({event_id:head.event_id,type:head.type,status:error.status});
          if(quarantined.length>128)quarantined.shift();
          if(head.type==='approval_required'&&pending.has(head.data.id)) pending.get(head.data.id).localOnly=true;
          outbox.shift();await state();continue;
        }
        if (head.type==='approval_required') {
          const slot = pending.get(head.data.id);
          if (slot) slot.approval_id = response.approval_id;
        }
        outbox.shift();
        await state();
      }
    })().finally(() => {syncing = undefined;});
    return syncing;
  }
  function emit(sid,type,data,key) {
    const event=fitEvent({session_id:sid,type,data,event_id:stableId(sid,key,type)});
    if(!event || outbox.length>=2048) return false;
    outbox.push(event);
    state().then(flush).catch(() => {}); // Heartbeat resumes persistent, idempotent deliveries.
    return true;
  }
  await startup('starting_loopback');
  const bridge = await modelService(ctx,directory,cfg);
  await startup('registering_tools');
  let ticking = false;
  async function deliverTurns() {
    for (const turn of await hub('/turns',{})) {
      const sid=turn.native_id;
      let outcome='rejected';
      if (!muted.has(sid) && (chosen.has(sid) || sid===fleetSessionId(cfg.token,turn.task_id))) {
        chosen.add(sid);
        await state();
        try {
          // Native DSH persists this request ID and deduplicates replay after a lost receipt.
          const result=await ctx.sessionController.prompt({sessionId:sid,requestId:turn.id,
            mode:'queue',content:[{type:'text',text:turn.prompt}]},abort.signal);
          if(result?.accepted) outcome='accepted';
        } catch { /* Keep raw desktop/provider errors out of the Hub. */ }
      }
      await hub('/turns/'+encodeURIComponent(turn.id),{state:outcome});
    }
  }
  async function tick() {
    if (ticking) return;
    ticking=true;
    try {
      await bridge.update();
      const {cancel_sessions=[],conversation_protocol} = await hub('/heartbeat',{version});
      for (const sid of cancel_sessions)
        if (chosen.has(sid)) await ctx.sessionController.cancel({sessionId:sid});
      await flush();
      if(conversation_protocol===1 && typeof ctx.sessionController.prompt==='function') await deliverTurns();
    } finally { ticking=false; }
  }
  const timer = setInterval(() => tick().catch(()=>{}),10000);
  timer.unref?.();
  tick().catch(()=>{});
  ctx.effect(()=>async () => {
    clearInterval(timer); abort.abort(); await bridge.close(); await persist.catch(()=>{});
  });

  ctx.on('session/event',(session,e) => {
    const sid = String(session.id);
    const d=e.data??{}, seq=e.seq;
    if (!chosen.has(sid) && !(e.type==='approval/decided' && pending.get(d.id)?.sid===sid)) return;
    if(e.type==='user/message') {
      const text=redact((d.content??[]).filter(b=>b.type==='text').map(b=>b.text).join(''));
      if(text) emit(sid,'user_message',{text,request_id:d.source?.rpcId},seq);
    }
    if(e.type==='turn/start') emit(sid,'status',{status:'running'},seq);
    if(e.type==='turn/end') {
      const kind=d.reason?.kind;
      emit(sid,'status',{status:kind==='completed'?'completed':kind==='aborted'?'cancelled':'failed'},seq);
    }
    if(e.type==='assistant/message') {
      const text=redact((d.message?.content??[]).filter(b=>b.type==='text').map(b=>b.text).join(''));
      if (text) {emit(sid,'message',{text},seq);emit(sid,'result',{text},seq);}
    }
    if(e.type==='tool/call') {
      const command=summarizeCall(d.name,d.arguments);
      if(d.callId) commands.set(stableId(sid,d.callId),{name:d.name,complete:command.complete,
        display:command.complete?command.display:undefined});
      emit(sid,'tool_call',{name:safeText(d.name),command:command.display,call_id:d.callId},seq);
    }
    if(e.type==='tool/result') {
      const call=d.message?.toolCallId;
      commands.delete(stableId(sid,call));
      emit(sid,'tool_result',{call_id:call,success:!d.message?.isError},seq);
    }
    if(e.type==='approval/asked') {
      const slot={id:d.id,sid,tool:d.toolName,call:d.callId,reason:String(d.reason||d.toolName),
        seq,claimed:false,expires:Date.now()/1000+120};
      pending.set(d.id,slot);
    }
    if(e.type==='approval/decided') {
      const slot=pending.get(d.id);pending.delete(d.id);
      if(!slot?.localOnly)emit(sid,'approval_resolved',{id:d.id,decision:d.outcome==='allowed-once'?'approve_once':'deny',
        cause:safeText(d.outcome)},seq);
    }
  });

  ctx.on('approval/request',async (req,next) => {
    const sid=idOf(req.agent);
    if(!chosen.has(sid)) return next();
    const slots=[...pending.values()].filter(v=>!v.claimed&&v.sid===sid&&v.tool===req.toolName&&v.call===req.callId);
    if(slots.length!==1) return 'rejected';
    const slot=slots[0]; slot.claimed=true;
    const command=commands.get(stableId(sid,req.callId));
    let workspace;
    try {workspace=await ctx.workingDirectory.ensure(req.agent,req.signal);} catch { /* Use native approval. */ }
    slot.localOnly = !workspace || redact(workspace)!==workspace || !command?.complete || command.name!==slot.tool ||
      redact(slot.reason)!==slot.reason || !emit(sid,'approval_required',{id:slot.id,command:command.display,
        workspace,reason:slot.reason,expires_at:slot.expires},slot.seq);
    if(slot.localOnly)emit(sid,'log',{text:'请在桌面处理本次审批。完整操作或工作目录缺失、含敏感字段或超出同步上限。'},slot.seq);
    if(slot.localOnly) return next();
    const stop=new AbortController();
    const signal=AbortSignal.any([stop.signal,abort.signal,...(req.signal?[req.signal]:[])]);
    const remote=async()=>{
      while(!signal.aborted && Date.now()/1000<slot.expires) {
        if(!chosen.has(sid))return 'rejected';
        if(slot.localOnly)return remoteWait(signal);
        if(slot.approval_id) {
          const decision = await hub('/decisions/'+encodeURIComponent(slot.approval_id));
          if(decision.state==='expired' || decision.decision==='deny') return 'rejected';
          if(decision.state==='decided'&&decision.decision==='approve_once') return 'allowed-once';
        }
        await delay(250,undefined,{signal});
      }
      return 'rejected';
    };
    const local=Promise.resolve().then(next).then(v=>v==='unavailable'?remoteWait(signal):v);
    try { return await Promise.race([local,remote()]); }
    catch { return 'rejected'; }
    finally {stop.abort();}
  },true);

  ctx.commands.register({name:'fleet',description:'Fleet 会话同步、节点、文件与派单',recordInput:false,
    input:{hint:'on | off | nodes | files <node> <volume> [path] | preview <node> <volume> <path> | dispatch <node> <prompt>'},
    handler:async ({agent,rawInput})=>{
      const sid=idOf(agent), args=rawInput.trim().split(/\s+/), op=args.shift();
      try {
        if(op==='on'||op==='off') {
          if(op==='on') {chosen.add(sid);muted.delete(sid);} else {chosen.delete(sid);muted.add(sid);}
          await state();return {kind:'success',text:op==='on'?'本会话已连接 Fleet':'本会话已停止同步'};
        }
        if(op==='nodes') return {kind:'success',text:JSON.stringify(await hub('/nodes'))};
        if(op==='files'||op==='preview') {
          const [target,volume,...rest]=args;
          if(!target||!volume) throw new Error('需要节点和卷名');
          const query=new URLSearchParams({path:rest.join(' '),action:op==='files'?'list':'preview'});
          return {kind:'success',text:JSON.stringify(await hub('/files/'+encodeURIComponent(target)+'/'+encodeURIComponent(volume)+'?'+query))};
        }
        if(op==='dispatch') {
          const node_id=args.shift(), prompt=args.join(' ');
          if(!node_id||!prompt) throw new Error('需要节点和任务内容');
          const result=await hub('/dispatch',{node_id,prompt});
          return {kind:'success',text:JSON.stringify(result)};
        }
        return {kind:'error',text:'可用命令：on、off、nodes、files、preview、dispatch'};
      } catch(e) {return {kind:'error',text:safeText(e.message)};}
    }});

  const require=createRequire(process.argv[1]);
  const {defineTool}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')).href);
  const textParam=description=>({type:'string',description,required:true});
  function register(name,description,parameters,execute) {
    ctx.tools.register(defineTool({name,description,parameters,
      output:{schema:{type:'string'},render:(_args,value)=>[{type:'text',text:value}]},
      async execute(args,execution) {
        if(!execution.agent || !chosen.has(idOf(execution.agent))) throw new Error('Enable this session with /fleet on');
        return safeText(JSON.stringify(await execute(args,execution)));
      }}));
  }
  register('fleet_nodes','查看授权 Fleet 节点与负载',{},()=>hub('/nodes'));
  register('fleet_files','列出授权节点内的命名卷目录，或预览文本文件',
    {node:textParam('目标节点 ID'),volume:textParam('卷名'),path:{type:'string'},action:{type:'string'}},
    ({node,volume,path='',action='list'})=>{
      if(!['list','preview'].includes(action)) throw new Error('Unknown file operation');
      return hub('/files/'+encodeURIComponent(node)+'/'+encodeURIComponent(volume)+'?'+new URLSearchParams({path,action}));
    });
  register('fleet_dispatch','在当前用户明确批准后向已授权节点派单',
    {node_id:textParam('目标节点 ID'),prompt:textParam('完整任务'),workspace:{type:'string'}},
    async (args,execution)=>{
      const service=ctx.get('approval');
      if(!service) throw new Error('Native approval service unavailable');
      const outcome=await service.request({agent:execution.agent,callId:execution.callId,signal:execution.signal,
        toolName:'fleet_dispatch',reason:safeText('派往 '+args.node_id+' '+(args.workspace||'')+' '+args.prompt)});
      if(outcome!=='allowed-once') throw new Error('Dispatch was not approved');
      return hub('/dispatch',args);
    });
  await startup('tools_registered');
}

function remoteWait(signal) {
  return new Promise(resolve=>{
    if(signal.aborted) resolve('cancelled');
    else signal.addEventListener('abort',()=>resolve('cancelled'),{once:true});
  });
}
