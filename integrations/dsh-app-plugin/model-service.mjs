import http from 'node:http';
import {channel} from 'node:diagnostics_channel';
import {AsyncLocalStorage} from 'node:async_hooks';
import {createHmac,randomBytes,timingSafeEqual} from 'node:crypto';
import {once} from 'node:events';
import {writeFile,rename,readFile,unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {hasHandoff,importConversation,InvalidTranscript} from './handoff.mjs';

export async function writePrivate(path,value) {
  const temporary = path + '.' + randomBytes(12).toString('hex') + '.tmp';
  try {
    await writeFile(temporary,JSON.stringify(value),{flag:'wx',mode:0o600});
    await rename(temporary,path);
  } finally { await unlink(temporary).catch(() => {}); }
}

function equal(a,b) {
  const x = Buffer.from(a), y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x,y);
}

// Associate diagnostics with request objects at creation, not the response callback's
// async context: concurrent calls may share one HTTP connection.
function watchInferenceTransport() {
  const scope = new AsyncLocalStorage();
  const requests = new WeakMap();
  function start({request}) {
    const note = scope.getStore();
    if (!note || request?.method !== 'POST' || typeof request.path !== 'string') return;
    const path = request.path.split('?', 1)[0];
    // Count only model invocation endpoints. Auth, model lists, title calls outside
    // this context, and unknown custom transport layouts cannot establish this evidence.
    if (/\/(?:messages|(?:chat\/)?completions|responses)\/?$|:(?:streamGenerateContent|generateContent)\/?$/.test(path)) {
      requests.set(request, note);
    }
  }
  function sent({request}) { requests.get(request)?.('upstream_send'); }
  function headers({request,response}) {
    const note = requests.get(request);
    const status = response?.statusCode;
    if (note && Number.isInteger(status) && status >= 100 && status <= 599) {
      note('upstream_http', {status});
    }
  }
  const listeners = [
    ['undici:request:create', start], ['undici:request:bodySent', sent],
    ['undici:request:headers', headers], ['http.client.request.start', start],
    ['http.client.response.finish', headers],
  ];
  for (const [name,handler] of listeners) channel(name).subscribe(handler);
  return {
    run: (note,fn) => scope.run(note,fn),
    close() {
      for (const [name,handler] of listeners) channel(name).unsubscribe(handler);
      scope.disable();
    },
  };
}

// Only match SDK-defined error codes, never an exception message, cause string,
// provider name, response body, upstream request ID or credential reference.
function failureCategory(error, status) {
  if (status === 401) return 'unauthorized';
  if (status === 403) return 'forbidden';
  if (status === 429) return 'rate_limit';
  if (status === 408 || status === 504) return 'timeout';
  if (status >= 500) return 'provider_unavailable';
  const code = error?.failure?.code ?? error?.code;
  switch (code) {
    case 'AUTH': case 'ACCOUNT_SIGN_IN_REQUIRED': case 'ACCOUNT_TOKEN_INVALID': return 'unauthorized';
    case 'NO_CREDENTIAL_STORE': case 'INVALID_CREDENTIAL': case 'MISSING_CREDENTIAL': return 'credentials_missing';
    case 'RATE_LIMIT': case 'QUOTA': return 'rate_limit';
    case 'TIMEOUT': case 'LLM_STREAM_IDLE_TIMEOUT': case 'ETIMEDOUT': return 'timeout';
    case 'TRANSPORT': case 'ECONNRESET': case 'ECONNREFUSED': case 'ENOTFOUND': return 'connection';
    case 'ABORTED': return 'aborted';
    case 'CONTENT_REFUSED': return 'model_refused';
    case 'SERVER': case 'NO_ADAPTER': case 'UNKNOWN_MODEL': case 'MODEL_UNSUPPORTED': return 'provider_unavailable';
    case 'INVALID_REQUEST': case 'UNSUPPORTED_CONTENT': case 'UNSUPPORTED_OPTION': return 'invalid_request';
    case 'MALFORMED_RESPONSE': case 'INVALID_RESPONSE': case 'EMPTY_RESPONSE': return 'protocol';
    default: return error?.name === 'AbortError' ? 'aborted' : 'provider_error';
  }
}

const failureCodes = new Map([
  ['unauthorized','AUTH'],['forbidden','AUTH'],['rate_limit','RATE_LIMIT'],['timeout','TIMEOUT'],
  ['connection','TRANSPORT'],['aborted','ABORTED'],['model_refused','CONTENT_REFUSED'],
  ['invalid_request','INVALID_REQUEST'],['credentials_missing','MISSING_CREDENTIAL'],['provider_unavailable','SERVER'],
]);

// Deliver the SDK's structured error without its provider message, request ID,
// body, headers or replay payload. The status here is SDK-reported; only a
// transport event can produce the upstream_http diagnostic.
function safeFailure(reason, error, category) {
  const failure = {code:failureCodes.get(category) ?? 'UNKNOWN',message:'Desktop model request failed'};
  if (Number.isInteger(error?.status) && error.status >= 100 && error.status <= 599) failure.status = error.status;
  return {type:'finish',reason:{kind:reason === 'aborted' ? 'aborted' : 'error',failure}};
}

// Keep a bounded per-Host-generation snapshot; slow or unavailable disk logging
// neither stalls inference nor accumulates an unbounded backlog of writes.
function diagnosticLog(directory) {
  const rows = [];
  let dirty = false, writing;
  function record(data) {
    rows.push({at:Date.now(), ...data});
    if (rows.length > 128) rows.shift();
    dirty = true;
    if (!writing) {
      writing = (async () => {
        while (dirty) {
          dirty = false;
          await writePrivate(join(directory,'model-diagnostics.json'),rows).catch(() => {});
        }
        writing = undefined;
      })();
    }
  }
  return {record, flush: () => writing};
}


export async function modelService(ctx,directory,pairing) {
  const diagnostics = diagnosticLog(directory);
  const token = randomBytes(40).toString('base64url');
  const handoffKey = pairing?.token &&
    createHmac('sha256',pairing.token).update('handoff/v1/'+token).digest('hex');
  let imports = 0, previousImport = Promise.resolve();
  const file = join(directory,'bridge.json');
  let port, previous;
  const active = new Set();
  const server = http.createServer(async (req,res) => {
    const abort = new AbortController();
    res.once('close',() => abort.abort());
    const reject = status => {res.writeHead(status);res.end();};
    if (req.socket.remoteAddress !== '127.0.0.1' || req.headers.host !== '127.0.0.1:'+port ||
        req.headers.origin) return reject(403);
    if (req.url === '/selection' && req.method === 'GET') {
      const nonce = req.headers['x-fleet-nonce'];
      const sign = value => createHmac('sha256', token).update(value).digest('hex');
      if (typeof nonce !== 'string' || !/^[a-f0-9]{48}$/.test(nonce) ||
          !equal(req.headers['x-fleet-proof'] ?? '', sign('request/' + nonce))) return reject(403);
      try {
        const {provider,model} = ctx.agentDefaultModel.currentSelection();
        if(typeof provider!=='string' || !provider || typeof model!=='string' || !model) return reject(503);
        res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store','X-Fleet-Model-Diagnostics':'1',
          ...(handoffKey && hasHandoff(ctx) ? {'X-Fleet-Desktop-Handoff':'1'} : {})});
        const proof = sign('response/' + JSON.stringify([nonce, provider, model]));
        return res.end(JSON.stringify({provider,model,proof}));
      } catch { return reject(503); }
    }
    if (req.url === '/handoff' && req.method === 'POST') {
      if (!handoffKey || !equal(req.headers.authorization ?? '', 'Bearer '+handoffKey)) return reject(403);
      if (!hasHandoff(ctx)) return reject(503);
      if (imports >= 8) return reject(409);
      imports++;
      try {
        let bytes = 0;
        const parts = [];
        for await (const part of req) {
          if ((bytes += part.length) > 3*1024*1024) return reject(413);
          parts.push(part);
        }
        const body = JSON.parse(Buffer.concat(parts).toString('utf8'));
        const write = previousImport.then(() => importConversation(ctx,pairing.token,body));
        previousImport = write.catch(() => {});
        const result = await write;
        res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});
        res.end(JSON.stringify(result));
      } catch (error) { reject(error instanceof SyntaxError || error instanceof InvalidTranscript ? 422 : 503); }
      finally { imports--; }
      return;
    }
    if (!equal(req.headers.authorization ?? '', 'Bearer '+token)) return reject(403);
    if (req.url !== '/model' || req.method !== 'POST') return reject(404);
    const acceptsDiagnostics = req.headers['x-fleet-model-diagnostics'] === '1';
    const suppliedTrace = req.headers['x-fleet-trace'];
    const trace = acceptsDiagnostics && typeof suppliedTrace === 'string' && /^[0-9a-f]{32}$/.test(suppliedTrace)
      ? suppliedTrace : randomBytes(16).toString('hex');
    res.setHeader('X-Fleet-Model-Diagnostics','1');
    let lastStatus, ended = false;
    let transportEvents = 0;
    const note = (stage, fields = {}) => {
      if (ended) return;
      // Bound transport retry events without dropping the terminal outcome.
      if (stage === 'upstream_http') lastStatus = fields.status;
      if ((stage === 'upstream_http' || stage === 'upstream_send') && ++transportEvents > 24) return;
      const data = {trace, stage, ...fields};
      diagnostics.record(data);
      if (acceptsDiagnostics && res.headersSent && !res.writableEnded && !res.destroyed) {
        res.write(JSON.stringify({type:'fleet_diagnostic_v1', ...data})+'\n');
      }
    };
    const refuse = (status,category) => {
      note('bridge_error',{category});
      return reject(status);
    };
    note('bridge_received');
    if (active.size >= 4) return refuse(429,'busy');
    active.add(abort);
    try {
      let size = 0;
      const body = [];
      for await (const part of req) {
        if ((size += part.length) > 8*1024*1024) return refuse(413,'invalid_request');
        body.push(part);
      }
      const data = JSON.parse(Buffer.concat(body).toString('utf8'));
      const {provider,model} = ctx.agentDefaultModel.currentSelection();
      if (provider !== data.provider || model !== data.model) return refuse(409,'route_changed');
      if (!Array.isArray(data.messages) || !data.messages.length) return refuse(400,'invalid_request');
      const options = {provider,model,signal:abort.signal};
      for (const field of ['messages','tools','system','temperature','maxTokens','stop','reasoningEffort'])
        if (data[field] !== undefined) options[field] = data[field];
      res.writeHead(200,{'Content-Type':'application/x-ndjson','Cache-Control':'no-store','X-Fleet-Model-Diagnostics':'1'});
      await transport.run(note, async () => {
        note('adapter_start');
        let first = true, finished = false;
        for await (const chunk of ctx.llm.stream(options)) {
          if (first) { note('adapter_chunk'); first = false; }
          if (!chunk || typeof chunk.type !== 'string' || finished ||
              chunk.type === 'fleet_diagnostic_v1' || chunk.type === 'bridge_error') {
            throw Object.assign(new Error(),{code:'MALFORMED_RESPONSE'});
          }
          let output = chunk;
          const reason = chunk.reason;
          if (chunk.type === 'error' || (chunk.type === 'finish' && ['error','aborted'].includes(reason?.kind))) {
            const error = reason?.failure ?? chunk.error;
            const category = reason?.kind === 'aborted' ? 'aborted' : failureCategory(error,lastStatus ?? error?.status);
            note('bridge_error',{category});
            output = safeFailure(reason?.kind,error,category);
            finished = true;
          } else if (chunk.type === 'finish') {
            note('adapter_end');
            finished = true;
          }
          if (!res.write(JSON.stringify(output)+'\n')) await once(res,'drain',{signal:abort.signal});
        }
        if (!finished) throw Object.assign(new Error(),{code:'MALFORMED_RESPONSE'});
      });
      res.end();
    } catch (error) {
      if (!res.headersSent) return refuse(400,'invalid_request');
      const category = failureCategory(error,lastStatus);
      note('bridge_error',{category});
      if (!res.destroyed) res.end('{"type":"bridge_error"}\n');
    } finally { ended = true; active.delete(abort); }
  });
  server.listen(0,'127.0.0.1');
  await once(server,'listening');
  const transport = watchInferenceTransport();
  port = server.address().port;
  async function update() {
    // The desktop profile can load before its default model service has a selection.
    let selection;
    try { selection = ctx.agentDefaultModel.currentSelection(); } catch { /* Retry on the next tick. */ }
    const {provider,model} = selection ?? {};
    if (typeof provider !== 'string' || !provider || typeof model !== 'string' || !model) {
      if (previous !== undefined) {
        await removeDescriptor();
        previous = undefined;
      }
      return;
    }
    if (previous === JSON.stringify([provider,model])) return;
    await writePrivate(file,{url:'http://127.0.0.1:'+port,provider,model,token});
    previous = JSON.stringify([provider,model]);
  }
  async function removeDescriptor() {
    try {
      if (JSON.parse(await readFile(file,'utf8')).token === token) await unlink(file);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  async function close() {
    transport.close();
    for (const controller of active) controller.abort();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await diagnostics.flush();
    await removeDescriptor().catch(() => {});
  }
  try { await update(); } catch (e) { await close(); throw e; }
  return {update,close};
}
