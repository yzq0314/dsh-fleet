import {createHmac} from 'node:crypto';
import {createRequire} from 'node:module';
import {basename, isAbsolute} from 'node:path';
import {pathToFileURL} from 'node:url';
import {isDeepStrictEqual} from 'node:util';

export class InvalidTranscript extends Error {}
function check(value) { if (!value) throw new InvalidTranscript('Unsupported Fleet transcript'); }
const nonempty = v => typeof v === 'string' && v.length > 0;

// An inert history seed. Only visible messages and historical tool results
// enter the desktop. No source-agent hooks, permissions or commands are replayed.
function seed(body, title) {
  const {messages,provider,model} = body;
  check(Array.isArray(messages) && messages.length >= 2 && messages.length <= 1024);
  check(nonempty(provider) && nonempty(model) && messages[0]?.role === 'user' && messages.at(-1)?.role === 'assistant');
  const events = [], calls = new Map(), messageIds = new Set(), callIds = new Set();
  const turn = 1;
  let step = 1, closed = false, time = messages[0].time;
  const add = (type,data,extra) => {
    const seq = events.length;
    events.push({type,data,seq,time,...extra});
    return seq;
  };
  add('turn/start',{turn});
  add('step/start',{turn,step});
  // Desktop replaces the protected first surface entry when it assembles the
  // next turn. Empty content reserves that entry without importing a prompt.
  let headId = 'fleet-system-' + body.task_id;
  while (messages.some(row => row?.id === headId)) headId += '-';
  messageIds.add(headId);
  add('system/message', {turn, step, message: {
    id: headId, role: 'system', source: {kind:'system-prompt'}, content:[]
  }}, {surfaceOp:'append'});
  for (const row of messages) {
    check(nonempty(row.id) && !messageIds.has(row.id) && Number.isSafeInteger(row.time) && row.time >= 0);
    messageIds.add(row.id);
    time = Math.max(time,row.time);
    check(Array.isArray(row.content));
    const content = row.content.map(block => {
      if (block.type === 'text' && typeof block.text === 'string') return {type:'text',text:block.text};
      check(block.type === 'tool-call' && row.role === 'assistant' && nonempty(block.id) &&
            nonempty(block.name) && typeof block.arguments === 'string' && !callIds.has(block.id));
      callIds.add(block.id);
      return {type:'tool-call',id:block.id,name:block.name,arguments:block.arguments};
    });
    const message = {id:row.id,role:row.role,content};
    if (row.role === 'user') {
      check(calls.size === 0);
      add('user/message',{...message,source:{kind:'user'}},{surfaceOp:'append'});
    } else if (row.role === 'assistant') {
      check(calls.size === 0);
      if (closed) {
        add('step/end',{turn,step});
        step++;
        add('step/start',{turn,step});
      }
      add('assistant/message',{turn,step,message:{...message,source:{kind:'model',provider,model}},stream:[]},{surfaceOp:'append'});
      for (const block of content) {
        if (block.type === 'tool-call') {
          calls.set(block.id,add('tool/call',{turn,step,callId:block.id,name:block.name,arguments:block.arguments}));
        }
      }
      closed = true;
    } else {
      check(row.role === 'tool' && typeof row.is_error === 'boolean' && calls.has(row.call_id));
      const callId = row.call_id;
      add('tool/result',{turn,step,message:{...message,toolCallId:callId,isError:row.is_error,source:{kind:'tool',callId}}},
          {sourceEventSeqs:[calls.get(callId)],surfaceOp:'append'});
      calls.delete(callId);
    }
  }
  check(calls.size === 0);
  add('step/end',{turn,step});
  add('turn/end',{turn,reason:{kind:'completed'}});
  add('session/title',{title,messageSeqs:[],source:{kind:'user'}});
  return events;
}

async function store(persistence,header,events) {
  let handle, existing = false;
  try {
    handle = await persistence.create(header);
  } catch (error) {
    if (error.name !== 'SessionAlreadyExistsError') throw error;
    existing = true;
    handle = await persistence.open(header.id,'read');
  }
  try {
    if (existing) {
      const {events:previous} = await handle.read(0,events.length);
      check(handle.header.cwd === header.cwd && handle.header.createdAt === header.createdAt &&
            isDeepStrictEqual(previous,events.slice(0,previous.length)));
      if (previous.length === events.length) return false;  // Also permits a subsequent Desktop turn.
      await handle.close();
      handle = await persistence.open(header.id,'write');
      const {events:locked} = await handle.read(0,events.length+1);
      check(locked.length <= events.length && isDeepStrictEqual(locked,events.slice(0,locked.length)));
      await handle.append(events.slice(locked.length));
    } else {
      await handle.append(events);
    }
    await handle.flush();
    return true;
  } finally { await handle.close(); }
}

export function hasHandoff(ctx) {
  return !!(ctx.get?.('workspaceRegistry')?.create && ctx.get?.('sessionPersistence')?.create);
}

export function fleetSessionId(pairKey,taskId) {
  const hash = createHmac('sha256',pairKey).update('session/v1/' + taskId).digest('hex');
  return [hash.slice(0,8),hash.slice(8,12),'5'+hash.slice(13,16),'a'+hash.slice(17,20),hash.slice(20,32)].join('-');
}

export async function importConversation(ctx,pairKey,body) {
  check(body && typeof body.task_id === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(body.task_id));
  check(typeof body.workspace === 'string' && isAbsolute(body.workspace));
  const first = body.messages?.[0]?.content?.find(b => b.type === 'text')?.text;
  check(typeof first === 'string');
  let title = 'Fleet · ';
  for (const char of first.split(/\r?\n/).find(line => line.trim())?.trim() || body.task_id) {
    if (Buffer.byteLength(title + char) > 80) break;
    if (char.codePointAt(0) >= 32) title += char;
  }
  const events = seed(body,title);
  const id = fleetSessionId(pairKey,body.task_id);
  const require = createRequire(process.argv[1]);
  const {Session,SESSION_FORMAT_VERSION} = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-session')).href);
  const ws = await ctx.get('workspaceRegistry').create(body.workspace,basename(body.workspace) || body.workspace);
  const header = {version:SESSION_FORMAT_VERSION,id,createdAt:events[0].time,cwd:ws.path,isSeeded:false};
  Session.create(id,events,header);
  if (await store(ctx.get('sessionPersistence'),header,events)) {
    ctx.get('sessionProjectionCache')?.coldSnapshot(header,0,events);
  }
  await ws.attachSession(id);
  return {session_id:id,title};
}
