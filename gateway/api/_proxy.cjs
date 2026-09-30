'use strict';

const ROUTES = Object.freeze({
  '/': 'GET', '/health': 'GET', '/api/session': 'POST',
  '/api/state': 'GET', '/api/policy': 'GET', '/api/run': 'POST', '/api/reset': 'POST',
  '/approver/api/chat': 'GET', '/approver/api/tap': 'POST',
});
const MAX_BODY = {'/api/session':256, '/api/run':256, '/api/reset':256, '/approver/api/tap':1024};
const MAX_RESPONSE = 1024 * 1024;
const crypto = require('node:crypto');
const fs = require('node:fs');
const nodePath = require('node:path');
function failure(res, status, message, code) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.end(JSON.stringify(code ? {ok:false,message,code} : {ok:false,message}));
}
function headers(res, page) {
  res.setHeader('Cache-Control', 'private, no-store, max-age=0');
  res.setHeader('Pragma', 'no-cache');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Cross-Origin-Resource-Policy', 'same-origin');
  res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'");
}
function allowedQuery(path, raw) {
  let u;
  try { u = new URL(raw || path, 'https://gateway.invalid'); } catch { return false; }
  if (u.hash) return false;
  if (!u.search) return true;
  return path === '/' && u.search === '?auto=1';
}
function originFromEnv(env) {
  const raw = env.APPROVED_BACKEND_ORIGIN;
  if (!raw) throw Error('missing origin');
  const u = new URL(raw);
  if (u.protocol !== 'https:' || !u.hostname || u.username || u.password || u.search || u.hash || !(/^\/$/.test(u.pathname) || /^\/a\/[a-f0-9-]{36}\/$/.test(u.pathname))) throw Error('invalid origin');
  return u.origin + (u.pathname === '/' ? '' : u.pathname.slice(0, -1));
}
function getSession(req) {
  const value = req.headers['x-approved-session'];
  return typeof value === 'string' && /^[A-Za-z0-9_-]{24,256}$/.test(value) ? value : null;
}
async function proxy(req, res, path, env=process.env, fetcher=fetch, timeoutMs=15000) {
  headers(res, path === '/');
  if (!Object.hasOwn(ROUTES, path)) return failure(res, 404, 'Not found.');
  if (req.method !== ROUTES[path] && !(path === '/api/policy' && req.method === 'HEAD')) {res.setHeader('Allow', path === '/api/policy' ? 'GET, HEAD' : ROUTES[path]);return failure(res, 405, 'Method not allowed.');}
  if (!allowedQuery(path, req.url)) return failure(res, 400, 'Query not allowed.');
  if (path === '/') return servePage(res);
  let origin;
  try { origin = originFromEnv(env); } catch { return failure(res, 503, 'The demo is unavailable.'); }
  const key = env.APPROVED_GATEWAY_KEY;
  if (path !== '/health' && (typeof key !== 'string' || !key)) return failure(res, 503, 'The demo is unavailable.');
  const session = path !== '/' && path !== '/health' && path !== '/api/session' ? getSession(req) : null;
  if (path !== '/' && path !== '/health' && path !== '/api/session' && !session) return failure(res, 401, 'This session expired. Start a new one.');
  const upstreamHeaders = {'Accept': path === '/' ? 'text/html' : 'application/json'};
  if (path !== '/health') upstreamHeaders['X-Approved-Gateway'] = key;
  if (session) upstreamHeaders['X-Approved-Session'] = session;
  let body;
  if (req.method === 'POST') {
    if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] || '')) return failure(res, 415, 'JSON required.');
    if (req.headers['content-length'] && Number(req.headers['content-length']) > MAX_BODY[path]) return failure(res, 413, 'Request too large.');
    try {body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body === undefined ? {} : req.body);} catch {return failure(res, 400, 'Invalid JSON.');}
    if (Buffer.byteLength(body) > MAX_BODY[path]) return failure(res, 413, 'Request too large.');
    try {JSON.parse(body);} catch {return failure(res, 400, 'Invalid JSON.');}
    upstreamHeaders['Content-Type'] = 'application/json';
  } else if (req.headers['content-length'] && Number(req.headers['content-length']) > 0) return failure(res, 413, 'Body not allowed.');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const upstream = await fetcher(origin + path, {method:req.method,headers:upstreamHeaders,body,redirect:'manual',signal:controller.signal});
    if (upstream.status >= 300 && upstream.status < 400) return failure(res, 502, 'The demo did not answer. Retry in a moment.');
    if (!upstream.ok) {
      const status = [400,401,403,404,409,410,413,429,503].includes(upstream.status) ? upstream.status : 502;
      let code;
      try {
        if (['/api/run','/api/session'].includes(path) && upstream.headers.get('content-type')?.startsWith('application/json')) {
          const bytes = await readBounded(upstream, 4096);
          const parsed = JSON.parse(bytes.toString('utf8'));
          if (typeof parsed.code === 'string') code = parsed.code;
        }
      } catch { /* never pass an untrusted backend body through */ }
      const messages = {
        'live-unavailable':'The live AI judge is unavailable. Retry later.',
        'budget-exhausted':'The live AI demo allowance is used up. Existing approval buttons still work. Retry later.',
        'budget-unavailable':'The live AI allowance cannot be checked, so new runs are paused. Existing approval buttons still work.',
        'run-in-progress':'A run is already in progress. Finish it before running again.',
        'rate-limited':'The demo is at capacity. Retry shortly.',
        'starting':'The demo is starting. Retry shortly.',
        'session-starting':'The private session is still starting. Retry shortly.',
        'chat-unavailable':'The approver chat is still starting. Retry shortly.',
      };
      const fallback = {401:'This session expired. Start a new one.',403:'This action was refused.',409:'A run is already in progress.',410:'This session expired. Start a new one.',429:'The demo is at capacity. Retry shortly.',503:'The live demo is unavailable. Retry later.'};
      const safeRunCode = path === '/api/run' && status === 503 && ['starting','session-starting','chat-unavailable'].includes(code) ? code : undefined;
      return failure(res,status,messages[code] || fallback[status] || 'The demo could not answer. Retry.',safeRunCode);
    }
    const expected = path === '/' ? 'text/html' : 'application/json';
    if (!(upstream.headers.get('content-type') || '').toLowerCase().startsWith(expected)) return failure(res,502,'The demo returned an invalid response.');
    const bytes = await readBounded(upstream, path === '/api/policy' ? 128 * 1024 : MAX_RESPONSE);
    if (path === '/') {
      const page = bytes.toString('utf8');
      const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)];
      const styles = [...page.matchAll(/<style>([\s\S]*?)<\/style>/g)];
      if (scripts.length !== 1 || styles.length !== 1) return failure(res,502,'The demo returned an invalid page.');
      const hash = value => "'sha256-" + crypto.createHash('sha256').update(value,'utf8').digest('base64') + "'";
      res.setHeader('Content-Security-Policy', "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://approval.md; connect-src 'self'; img-src 'self' data:; script-src " + hash(scripts[0][1]) + '; style-src ' + hash(styles[0][1]));
    }
    res.statusCode=upstream.status;
    res.setHeader('Content-Type',expected+'; charset=utf-8');
    res.end(bytes);
  } catch {
    return failure(res,502,'The demo did not answer. Retry in a moment.');
  } finally {
    clearTimeout(timeout);
  }

}
function servePage(res) {
  let page;
  try {page=fs.readFileSync(nodePath.join(__dirname,'../page.html'),'utf8');}
  catch {return failure(res,503,'The demo page is unavailable.');}
  const scripts=[...page.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  const styles=[...page.matchAll(/<style>([\s\S]*?)<\/style>/g)];
  if (scripts.length!==1||styles.length!==1) return failure(res,503,'The demo page is unavailable.');
  const hash=value=>"'sha256-"+crypto.createHash('sha256').update(value,'utf8').digest('base64')+"'";
  res.setHeader('Content-Security-Policy',"default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://approval.md; connect-src 'self'; img-src 'self' data:; script-src "+hash(scripts[0][1])+'; style-src '+hash(styles[0][1]));
  res.statusCode=200;
  res.setHeader('Content-Type','text/html; charset=utf-8');
  res.end(page);
}
async function readBounded(response, limit) {
  const reader=response.body?.getReader();
  if (!reader) return Buffer.alloc(0);
  const chunks=[]; let size=0;
  try {
    for (;;) {
      const {done,value}=await reader.read();
      if (done) break;
      size+=value.byteLength;
      if (size>limit) {reader.cancel().catch(()=>{});throw Error('response too large');}
      chunks.push(Buffer.from(value));
    }
  } finally {reader.releaseLock();}
  return Buffer.concat(chunks,size);
}
module.exports={proxy,allowedQuery,originFromEnv};
