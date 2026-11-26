// Runs inside Electron's Node mode. Every Hub, model response and session in this host is a fixture.
import {join,resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const [modules,profilePackage]=process.argv.slice(2);
const load=(name)=>import(pathToFileURL(join(modules,name,'lib','index.js')).href);
const {Context}=await load('cordis');
const llm=await load('dsh-llm');
const provider=await load('dsh-llm-deepseek-api-key');
const {loadProfileDirectory}=await load('dsh-app-boot');
const profile=loadProfileDirectory('dsh',resolve(profilePackage,'../../..'),join(modules,'dsh-desktop-host','package.json'));
if(!profile.layers.some(layer=>layer.packageName==='@dsh-fleet/desktop'&&layer.patches.length))
  throw new Error('Native profile loader rejected installed fixture package: '+JSON.stringify(profile.skippedBundles));
const root=new Context();
await root.plugin((await load('dsh-storage')).Storage);
await root.plugin(await load('dsh-storage-json'),{root:join(process.env.DSH_HOME,'storage')});
await root.plugin(await load('dsh-storage-domain'),{backend:'json'});
await root.plugin((await load('dsh-session')).SessionStore);
await root.plugin(await load('dsh-session-persistence-jsonl'),{root:join(process.env.DSH_HOME,'sessions'),compression:'none'});
await root.plugin((await load('dsh-workspace')).WorkspaceRegistry);
await root.plugin((await load('dsh-session-query')).SessionQueryEngine);
await root.plugin(llm.LlmRuntime);
await root.plugin(provider,{baseURL:process.env.DEEPSEEK_BASE_URL,apiKeyEnv:'DEEPSEEK_API_KEY'});
const handlers=new Map(), registered=new Map();
const cancelled=[];
root.provide('commands',{register(v){handlers.set(v.name,v.handler);}});
root.provide('tools',{register(v){registered.set(v.name,v);}});
root.provide('sessionController',{
  cancel({sessionId}){cancelled.push(sessionId);},
  async prompt(request,signal) {
    signal.throwIfAborted();
    assert.equal(request.mode,'queue');
    assert.match(request.requestId,/^[0-9a-f]{32}$/);
    assert.deepEqual(request.content,[{type:'text',text:'continue in this workspace'}]);
    assert.ok(root.workspaceRegistry.list().some(w=>w.sessionIds.includes(request.sessionId)));
    return {accepted:true};
  },
});
root.provide('workingDirectory',{ensure(){return process.cwd();}});
root.provide('agentDefaultModel',{currentSelection(){return {provider:'deepseek-official',model:'deepseek-v4-pro'};}});
process.argv[1]=join(modules,'dsh-desktop-host','lib','index.js');
const plugin=await import(pathToFileURL(join(profilePackage,'index.mjs')).href);
await root.plugin(plugin);
await readFile(join(process.env.DSH_HOME,'fleet-desktop','bridge.json'));
if(!handlers.has('fleet') || !['fleet_nodes','fleet_files','fleet_dispatch'].every(n=>registered.has(n)))
  throw new Error('Desktop bindings not registered');
console.log('FLEET_FIXTURE_READY');
const input=createInterface({input:process.stdin});
for await (const command of input) {
  if(command.startsWith('reopen ')) {
    const id=command.slice(7);
    const history=await root.sessionQuery.inspectSession(id, session=>session.deriveMessages());
    assert.ok(history.some(m=>m.role==='user' && JSON.stringify(m.content).includes('Continue this test.')));
    assert.ok(history.some(m=>m.role==='tool'));
    console.log('FLEET_FIXTURE_REOPENED');
  }
  if(command.startsWith('continue ')) {
    const id=command.slice(9);
    const log=await root.sessionPersistence.open(id,'read');
    try {
      const {events}=await log.read(0,4);
      const head=events.find(e=>e.surfaceOp==='append');
      assert.equal(head?.type,'system/message');
      assert.deepEqual(head.data.message.content,[]);
    } finally { await log.close(); }
    const ws=root.workspaceRegistry.list().find(w=>w.sessionIds.includes(id));
    assert.ok(ws);
    assert.equal(resolve(ws.path).toLowerCase(),process.cwd().toLowerCase());
    assert.deepEqual((await root.sessionQuery.listSessions()).map(x=>x.header.id),[id]);
    const history=await root.sessionQuery.inspectSession(id,session=>session.deriveMessages());
    assert.deepEqual(history.map(m=>m.role),['user','assistant','tool','assistant']);
    const marker=await readFile(join(process.cwd(),'fixture-input.txt'),'utf8');
    assert.ok(JSON.stringify(history[2].content).includes(marker));
    console.log('FLEET_FIXTURE_OPENED');
  }
  if(command==='exercise') {
    const session={id:'selected',header:{cwd:process.cwd()}};
    const agent={session};
    const slash=arg=>handlers.get('fleet')({agent,rawInput:arg});
    root.emit('session/event',{id:'private'},{type:'turn/start',seq:1,data:{}});
    await slash('on');
    for(const line of ['nodes','files target share','dispatch target local fixture'])
      if((await slash(line)).kind!=='success') throw new Error('Slash command failed: '+line);
    root.emit('session/event',session,{type:'turn/start',seq:1,data:{}});
    root.emit('session/event',session,{type:'tool/call',seq:10,data:{name:'read',callId:'c',arguments:'{"path":"file"}'}});
    root.emit('session/event',session,{type:'approval/asked',seq:2,data:{id:'pending-1',toolName:'read',callId:'c'}});
    const answer=await root.waterfall('approval/request',{agent,toolName:'read',callId:'c'},async()=>'unavailable');
    if(answer!=='allowed-once') throw new Error('Remote approval mapping: '+answer);
    root.emit('session/event',session,{type:'approval/decided',seq:3,data:{id:'pending-1',outcome:answer}});
    root.emit('session/event',session,{type:'tool/call',seq:11,data:{name:'read',callId:'d',arguments:'{"path":"file"}'}});
    root.emit('session/event',session,{type:'approval/asked',seq:4,data:{id:'pending-2',toolName:'read',callId:'d'}});
    const local=await root.waterfall('approval/request',{agent,toolName:'read',callId:'d'},async()=>'rejected');
    if(local!=='rejected')throw new Error('Native rejection was lost');
    root.emit('session/event',session,{type:'approval/decided',seq:5,data:{id:'pending-2',outcome:local}});
    root.emit('session/event',session,{type:'assistant/message',seq:30,data:{message:{content:[{type:'text',text:'reject-fixture'}]}}});
    root.emit('session/event',session,{type:'assistant/message',seq:31,data:{message:{content:[{type:'text',text:'文'.repeat(40000)}]}}});
    root.emit('session/event',session,{type:'tool/call',seq:32,data:{name:'write',callId:'large',
      arguments:JSON.stringify({content:'文'.repeat(40000)})}});
    root.emit('session/event',session,{type:'approval/asked',seq:33,data:{id:'large',toolName:'write',callId:'large'}});
    const oversized=await root.waterfall('approval/request',{agent,toolName:'write',callId:'large'},async()=>'rejected');
    if(oversized!=='rejected')throw new Error('Unrepresentable request must use native approval');
    root.emit('session/event',session,{type:'approval/decided',seq:34,data:{id:'large',outcome:oversized}});
    root.emit('session/event',session,{type:'tool/call',seq:35,data:{name:'test',callId:'private-args',
      arguments:JSON.stringify({nested:{access_token:'fixture-censored-value'}})}});
    root.emit('session/event',session,{type:'approval/asked',seq:36,data:{id:'private-args',toolName:'test',callId:'private-args'}});
    const sensitive=await root.waterfall('approval/request',{agent,toolName:'test',callId:'private-args'},async()=>'rejected');
    if(sensitive!=='rejected')throw new Error('Sensitive request must use native approval');
    root.emit('session/event',session,{type:'approval/decided',seq:37,data:{id:'private-args',outcome:sensitive}});
    root.emit('session/event',session,{type:'turn/end',seq:6,data:{reason:{kind:'completed'}}});
    console.log('FLEET_FIXTURE_SYNCED');
  }
  if(command.startsWith('probe ')) {
    const nonce=Number(command.split(' ')[1]);
    root.emit('session/event',{id:'selected'},{type:'assistant/message',seq:nonce,
      data:{message:{content:[{type:'text',text:'durable-probe-'+nonce}]}}});
    console.log('FLEET_FIXTURE_PROBED');
  }
  if(command==='quit')break;
}
input.close();
process.stdin.pause();
await root.fiber.dispose();
process.exit(0);
