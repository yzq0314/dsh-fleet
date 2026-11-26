// The registry gives every execution a fresh token. A late or missing answer
// can never release a different execution in the synchronous final guard.
import net from 'node:net';
import readline from 'node:readline';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

export const name = 'fleet-execution-guard';
export const inject = ['tools'];

async function exchange(request, signal) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({
      host: '127.0.0.1', port: Number(process.env.FLEET_GUARD_PORT), signal
    });
    const lines = readline.createInterface({input: socket});
    socket.setTimeout(Number(process.env.FLEET_GUARD_TIMEOUT), () => socket.destroy());
    socket.once('error', reject);
    socket.once('close', () => reject(new Error('Fleet guard disconnected')));
    socket.once('connect', () => {
      socket.write(JSON.stringify({...request, token: process.env.FLEET_GUARD_KEY}) + '\n');
    });
    lines.once('line', line => {
      try { resolve(JSON.parse(line)); }
      catch (err) { reject(err); }
      finally { socket.destroy(); }
    });
  });
}

export async function apply(ctx) {
  const cleared = new Set();
  ctx.tools.guard(exec => cleared.delete(exec.token) ? undefined : 'Missing Fleet decision');
  ctx.on('tools/pre-execute', async (exec, next) => {
    try {
      if ((await exchange({kind: 'tool', name: exec.name, args: exec.arguments, call_id: exec.callId}, exec.signal)).allowed === true) {
        cleared.add(exec.token);
        return await next();
      }
    } catch { /* Disconnect and timeout both deny the pending operation. */ }
    return {kind: 'deny', reason: 'Fleet policy denied this tool call'};
  });
  const checks = JSON.parse(process.env.FLEET_CHECK_NAMES);
  const runners = JSON.parse(process.env.FLEET_RUNNERS || '[]');
  if (checks.length || runners.length) {
    const require = createRequire(process.argv[1]);
    const {defineTool} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-tools')).href);
    if (checks.length) ctx.tools.register(defineTool({
      name: 'fleet_check',
      description: 'Run one administrator-reviewed local workspace check. Available: ' + checks.join(', '),
      parameters: {name: {type: 'string', required: true, description: 'Exact registered check name'}},
      output: {schema: {type: 'string'}, render: (_args, result) => [{type: 'text', text: result}]},
      async execute(args, execution) {
        const response = await exchange({kind: 'run_check', name: args.name}, execution.signal);
        if (!response.allowed) throw new Error('Local check unavailable or changed');
        return response.result;
      }
    }));
    if (runners.length) ctx.tools.register(defineTool({
      name: 'fleet_run',
      description: 'Run a newly written workspace script inside an isolated guest. Supply a relative script path and explicitly list every input file. The guest sees only a read-only copy of these files; write results via stdout. Runtimes: ' + runners.join(', '),
      parameters: {
        script: {type: 'string', required: true, description: 'Relative path of the existing script'},
        language: {type: 'string', description: 'Runtime name, defaults to python'},
        inputs: {type: 'array', items: {type: 'string'}, description: 'Explicit relative paths for read-only inputs'},
        args: {type: 'array', items: {type: 'string'}, description: 'Guest argv, passed as individual values'}
      },
      output: {schema: {type: 'string'}, render: (_args, result) => [{type: 'text', text: result}]},
      async execute(args, execution) {
        const response = await exchange({kind: 'run_guest', args}, execution.signal);
        if (!response.allowed) throw new Error('Fleet denied this run or the isolated runtime is unavailable');
        return response.result;
      }
    }));
  }
  if (!(await exchange({kind: 'ready'})).allowed) throw new Error('Fleet guard handshake failed');
}
