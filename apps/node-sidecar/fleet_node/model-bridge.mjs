import {randomBytes} from 'node:crypto';
import {createRequire} from 'node:module';
import {createConnection} from 'node:net';
import {createInterface} from 'node:readline';
import {Readable} from 'node:stream';
import {pathToFileURL} from 'node:url';

export const name = 'fleet-model-bridge';
export const inject = ['llm'];

const stages = new Set(['adapter_start','upstream_send','upstream_http','adapter_chunk','adapter_end','bridge_error']);
const categories = new Set(['unauthorized','forbidden','rate_limit','timeout','connection','aborted','invalid_request',
  'route_changed','busy','protocol','provider_error','bridge_unavailable','model_refused','unknown',
  'credentials_missing','provider_unavailable']);

function report(trace,stage,fields = {}) {
  const port = Number(process.env.FLEET_GUARD_PORT), token = process.env.FLEET_GUARD_KEY;
  if (!Number.isInteger(port) || port < 1 || port > 65535 || !token) return;
  return new Promise(resolve => {
    const socket = createConnection({host:'127.0.0.1',port});
    const stop = () => socket.destroy();
    const timer = setTimeout(stop,500);
    socket.once('connect',() => socket.write(JSON.stringify({kind:'model_diagnostic',token,trace,stage,...fields})+'\n'));
    socket.once('data',stop);
    socket.once('error',stop);
    socket.once('close',() => { clearTimeout(timer); resolve(); });
  });
}

function diagnostic(frame, trace) {
  if (frame.trace !== trace || !stages.has(frame.stage)) return;
  if (frame.stage === 'upstream_http') {
    if (Number.isInteger(frame.status) && frame.status >= 100 && frame.status <= 599) return {status:frame.status};
  } else if (frame.stage === 'bridge_error') {
    if (categories.has(frame.category)) return {category:frame.category};
  } else return {};
}

const legacyCodes = new Set(['AUTH','RATE_LIMIT','QUOTA','TIMEOUT','TRANSPORT','ABORTED','INVALID_REQUEST',
  'CONTENT_REFUSED','MISSING_CREDENTIAL','SERVER','UNKNOWN','NO_ADAPTER','MODEL_UNSUPPORTED']);
function safeFinish(chunk) {
  const reason = chunk.reason;
  const failed = chunk.type === 'error' || (chunk.type === 'finish' && ['error','aborted'].includes(reason?.kind));
  if (!failed) return;
  const source = reason?.failure ?? chunk.error ?? {};
  const code = legacyCodes.has(source.code) ? source.code : 'UNKNOWN';
  const failure = {code, message:'Desktop model request failed'};
  if (Number.isInteger(source.status) && source.status >= 100 && source.status <= 599) failure.status = source.status;
  return {type:'finish',reason:{kind:reason?.kind === 'aborted' ? 'aborted' : 'error',failure}};
}

export async function* desktopStream(options) {
  const {signal, messages, tools, system, temperature, maxTokens, stop, reasoningEffort, model} = options;
  const trace = randomBytes(16).toString('hex');
  await report(trace,'client_start');
  let category = 'bridge_unavailable', localStatus, reported = false, input;
  try {
    const response = await fetch(process.env.FLEET_MODEL_BRIDGE + '/model', {
      method:'POST',redirect:'error',signal,
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+process.env.FLEET_MODEL_CAPABILITY,
        'X-Fleet-Model-Diagnostics':'1','X-Fleet-Trace':trace},
      body:JSON.stringify({provider:process.env.FLEET_SELECTED_PROVIDER,model,messages,tools,system,
        temperature,maxTokens,stop,reasoningEffort}),
    });
    const supportsDiagnostics = response.headers.get('x-fleet-model-diagnostics') === '1';
    if (supportsDiagnostics) await report(trace,'bridge_received');
    if (!response.ok) {
      localStatus = response.status;
      category = ({400:'invalid_request',401:'unauthorized',403:'forbidden',409:'route_changed',
        413:'invalid_request',429:'busy'})[localStatus] ?? 'bridge_unavailable';
      await response.body?.cancel();
      throw new Error();
    }
    category = 'protocol';
    input = Readable.fromWeb(response.body);
    let finished = false;
    for await (const line of createInterface({input,crlfDelay:Infinity})) {
      const chunk = JSON.parse(line);
      if (chunk.type === 'fleet_diagnostic_v1') {
        const fields = supportsDiagnostics && diagnostic(chunk,trace);
        if (!fields) throw new Error();
        await report(trace,chunk.stage,fields);
        if (chunk.stage === 'bridge_error') reported = true;
        continue;
      }
      if (chunk.type === 'bridge_error') {
        if (!reported) await report(trace,'bridge_error',{category:'unknown'});
        reported = true;
        throw new Error();
      }
      const failure = safeFinish(chunk);
      if (failure && !reported) {
        await report(trace,'bridge_error',{category:failure.reason.kind === 'aborted' ? 'aborted' : 'provider_error'});
        reported = true;
      }
      if (failure || chunk.type === 'finish') finished = true;
      yield failure ?? chunk;
    }
    if (!finished) throw new Error();
  } catch {
    if (!reported) await report(trace,'client_error',{category:signal?.aborted ? 'aborted' : category,
      ...(localStatus >= 400 && localStatus <= 599 ? {status:localStatus} : {})});
    throw new Error('Desktop model stream failed');
  } finally { input?.destroy(); }
}

export async function apply(ctx) {
  const require = createRequire(process.argv[1]);
  const {LlmAdapter} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href);
  class DesktopModel extends LlmAdapter {
    stream(options) { return desktopStream(options); }
  }
  ctx.llm.registerAdapter(['fleet-desktop'],new DesktopModel());
}
