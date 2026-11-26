import assert from 'node:assert/strict';
import {createHmac,randomBytes} from 'node:crypto';
import {once} from 'node:events';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import http from 'node:http';
import net from 'node:net';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {desktopStream} from '../apps/node-sidecar/fleet_node/model-bridge.mjs';
import {modelService} from '../integrations/dsh-app-plugin/model-service.mjs';

async function listen(server) {
  server.listen(0,'127.0.0.1');
  await once(server,'listening');
  return server.address().port;
}

function shutdown(server) {
  server.closeAllConnections?.();
  return new Promise(resolve => server.close(resolve));
}

test('desktop can load before a default model and publish its bridge when ready', async () => {
  const dir = await mkdtemp(join(tmpdir(),'fleet-late-selection-'));
  let selection;
  const ctx = {agentDefaultModel:{currentSelection() {
    if (!selection) throw new Error('not initialized');
    return selection;
  }}};
  let service;
  try {
    service = await modelService(ctx,dir);
    const descriptor = join(dir,'bridge.json');
    await assert.rejects(readFile(descriptor), {code:'ENOENT'});
    selection = {provider:'test-provider',model:'test'};
    await service.update();
    assert.equal(JSON.parse(await readFile(descriptor)).model,'test');
    selection = {};
    await service.update();
    await assert.rejects(readFile(descriptor), {code:'ENOENT'});
    selection = {provider:'test-provider',model:'next'};
    await service.update();
    assert.equal(JSON.parse(await readFile(descriptor)).model,'next');
  } finally {
    await service?.close();
    await rm(dir,{recursive:true,force:true});
  }
});

test('authenticated loopback diagnostics distinguish endpoint, SDK and local bridge errors', async (t) => {
  const directory = await mkdtemp(join(tmpdir(),'fleet-bridge-test-'));
  t.after(() => rm(directory,{recursive:true,force:true}));
  const privateText = 'sensitive-fixture-header-raw-body.example';
  const token = randomBytes(32).toString('hex');
  const events = [];
  const guard = net.createServer(socket => {
    socket.once('data',bytes => {
      const frame = JSON.parse(bytes);
      assert.equal(frame.token,token);
      assert.equal(frame.kind,'model_diagnostic');
      delete frame.token;
      events.push(frame);
      socket.end('{"allowed":true}\n');
    });
  });
  const guardPort = await listen(guard);
  t.after(() => shutdown(guard));

  const upstream = http.createServer((req,res) => {
    req.resume();
    const status = req.url.startsWith('/auth/') ? 418 : Number(req.headers['x-test-status']);
    res.writeHead(status);
    res.end(privateText);
  });
  const upstreamUrl = 'http://127.0.0.1:'+await listen(upstream);
  t.after(() => shutdown(upstream));
  const selected = {provider:'test-adapter',model:'test-model'};
  const ctx = {
    agentDefaultModel:{currentSelection:() => selected},
    llm:{async *stream({messages}) {
      const mode = messages[0].content;
      if (mode === 'local') throw Object.assign(new Error(privateText),{failure:{code:'MISSING_CREDENTIAL'}});
      if (mode === 'spoof') { yield {type:'fleet_diagnostic_v1',stage:'upstream_http',status:200}; return; }
      const status = mode === 'ok' ? 200 : 401;
      const headers = {'x-test-status':String(status),'Authorization':'Bearer '+privateText};
      await (await fetch(upstreamUrl+'/auth/token',{method:'POST',headers,body:privateText})).text();
      const response = await fetch(upstreamUrl+'/v1/messages?'+privateText,{method:'POST',headers,body:privateText});
      await response.text();
      if (mode === 'ok') {
        yield {type:'text-delta',text:'Synthetic reply'};
        yield {type:'finish',reason:{kind:'stop'}};
      } else {
        const failure = {code:'AUTH',message:privateText,status:401,requestId:privateText};
        if (mode === 'error') yield {type:'error',error:failure};
        else yield {type:'finish',reason:{kind:'error',failure},replayState:privateText};
      }
    }},
  };
  const model = await modelService(ctx,directory);
  let closed = false;
  t.after(() => closed ? undefined : model.close());
  const descriptor = JSON.parse(await readFile(join(directory,'bridge.json'),'utf8'));
  const nonce = randomBytes(24).toString('hex');
  const proof = createHmac('sha256',descriptor.token).update('request/'+nonce).digest('hex');
  const selection = await fetch(descriptor.url+'/selection',{headers:{'x-fleet-nonce':nonce,'x-fleet-proof':proof}});
  assert.equal(selection.status,200);
  assert.equal(selection.headers.get('x-fleet-model-diagnostics'),'1');
  await selection.text();
  Object.assign(process.env,{
    FLEET_GUARD_KEY:token,FLEET_GUARD_PORT:String(guardPort),FLEET_MODEL_CAPABILITY:descriptor.token,
    FLEET_MODEL_BRIDGE:descriptor.url,FLEET_SELECTED_PROVIDER:selected.provider,
  });
  async function collect(mode,modelName = selected.model) {
    return Array.fromAsync(desktopStream({model:modelName,messages:[{role:'user',content:mode}]}));
  }
  const [ok,denied,errored] = await Promise.all([collect('ok'),collect('finish-error'),collect('error')]);
  assert.equal(ok[0].text,'Synthetic reply');
  for (const chunks of [denied,errored]) {
    assert.deepEqual(chunks,[{type:'finish',reason:{kind:'error',failure:{
      code:'AUTH',message:'Desktop model request failed',status:401,
    }}}]);
  }
  await assert.rejects(collect('local'),/Desktop model stream failed/);
  await assert.rejects(collect('spoof'),/Desktop model stream failed/);
  await assert.rejects(collect('route','changed-model'),/Desktop model stream failed/);
  const legacy = await fetch(descriptor.url+'/model',{method:'POST',headers:{Authorization:'Bearer '+descriptor.token},
    body:JSON.stringify({...selected,messages:[{content:'ok'}]})});
  assert.deepEqual((await legacy.text()).trim().split('\n').map(line => JSON.parse(line).type),['text-delta','finish']);
  await model.close();
  closed = true;
  const disk = JSON.parse(await readFile(join(directory,'model-diagnostics.json'),'utf8'));
  assert.doesNotMatch(JSON.stringify([disk,events]),/sensitive-fixture|changed-model|test-model|test-adapter/);
  for (const row of [...disk,...events]) {
    assert.match(row.trace,/^[0-9a-f]{32}$/);
    assert.ok(Object.keys(row).every(key => ['kind','at','trace','stage','status','category'].includes(key)));
  }
  const traces = events.filter(e => e.stage === 'client_start').map(e => e.trace);
  assert.equal(new Set(traces).size,6);
  const results = [];
  for (const trace of traces) {
    const rows = events.filter(e => e.trace === trace);
    assert.ok(rows.some(e => e.stage === 'bridge_received'));
    const remote = rows.filter(e => e.stage === 'upstream_http').map(e => e.status);
    const journal = disk.filter(e => e.trace === trace);
    assert.deepEqual(remote,journal.filter(e => e.stage === 'upstream_http').map(e => e.status));
    const error = rows.find(e => e.stage === 'bridge_error' || e.stage === 'client_error');
    if (error?.category === 'route_changed') {
      assert.equal(error.status,409);
      assert.ok(!rows.some(e => e.stage === 'adapter_start'));
    }
    if (remote.length) assert.ok(rows.some(e => e.stage === 'upstream_send'));
    if (!error) assert.ok(rows.some(e => e.stage === 'adapter_end'));
    results.push([error?.category ?? 'success',...remote].join(' '));
  }
  assert.deepEqual(results.sort(),['credentials_missing','protocol','route_changed','success 200','unauthorized 401','unauthorized 401'].sort());
});
