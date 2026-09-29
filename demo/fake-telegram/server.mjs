#!/usr/bin/env node
/**
 * A fake Telegram Bot API plus a one-page chat view, for the Approved demo only.
 *
 * Derived from approval-md-hosted `scripts/smoke/fake-telegram.mjs` (Bountify, MIT), which
 * answers the calls the approval.md Telegram channel makes (getMe, getWebhookInfo,
 * setWebhook, deleteWebhook, sendMessage, editMessageText, answerCallbackQuery). Added here:
 *
 * - The chat view at GET /: every message any bot sent to the demo chat, in order. The
 *   daemon's approval prompts carry their Approve/Reject buttons; the judge's advisories sit
 *   in the same thread. Message text is rendered through a tag allowlist, never as raw HTML.
 * - Taps: POST /api/tap {message_id, data} builds the `callback_query` update Telegram would
 *   send when the configured human (DEMO_SENDER_ID) taps that button, and delivers it the way
 *   Telegram does in webhook mode: POST to the daemon's webhook with the
 *   `X-Telegram-Bot-Api-Secret-Token` the daemon registered through setWebhook. The daemon's
 *   registered URL is a public placeholder, so delivery goes to FORWARD_WEBHOOK_URL, the
 *   daemon's address on the compose network.
 * - GET /api/chat: the same thread as JSON, for demo/smoke.sh to assert on.
 *
 * It never prints or reports a bot token (it is in the request path) or the webhook secret.
 */
import { createServer } from "node:http";

const port = Number(process.env.FAKE_TG_PORT ?? "8081");
const chatId = String(process.env.DEMO_CHAT_ID ?? "4242");
const senderId = Number(process.env.DEMO_SENDER_ID ?? "4242");
const forwardUrl = process.env.FORWARD_WEBHOOK_URL ?? "";

const calls = [];
const webhooks = {}; // bot id -> { url, secret }
const messages = []; // { bot, message_id, chat_id, text, buttons: [{text, data}], edited, at }
let messageId = 0;
let updateId = 1000;

const botIdOf = (token) => {
  const id = token.split(":")[0] ?? "";
  return /^[0-9]{1,20}$/u.test(id) ? id : "777000111";
};

const buttonsOf = (markup) => {
  const rows =
    markup && typeof markup === "object" && Array.isArray(markup.inline_keyboard)
      ? markup.inline_keyboard
      : [];
  return rows
    .flat()
    .filter((b) => b && typeof b.callback_data === "string")
    .map((b) => ({ text: typeof b.text === "string" ? b.text : b.callback_data, data: b.callback_data }));
};

const json = (res, status, body) => {
  const bytes = Buffer.from(JSON.stringify(body), "utf8");
  res.writeHead(status, { "content-type": "application/json", "content-length": bytes.length });
  res.end(bytes);
};

const readBody = (req) =>
  new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(raw.length === 0 ? {} : JSON.parse(raw));
      } catch {
        resolve({});
      }
    });
  });

/** Deliver a tap on one of a bot's buttons to that bot's registered webhook. */
async function tap(messageIdToTap, data) {
  const message = messages.find((m) => m.message_id === messageIdToTap);
  if (!message) return { status: 404, body: { ok: false, error: "no such message" } };
  if (!message.buttons.some((b) => b.data === data)) {
    return { status: 400, body: { ok: false, error: "that button is not on that message" } };
  }
  const hook = webhooks[message.bot];
  if (!hook || !hook.url || !hook.secret) {
    return { status: 409, body: { ok: false, error: "the bot has no webhook registered yet" } };
  }
  if (!forwardUrl) return { status: 500, body: { ok: false, error: "FORWARD_WEBHOOK_URL unset" } };
  updateId += 1;
  const update = {
    update_id: updateId,
    callback_query: {
      id: `cb-${updateId}`,
      from: { id: senderId, is_bot: false, first_name: "Demo approver" },
      chat_instance: `ci-${chatId}`,
      message: {
        message_id: message.message_id,
        date: Math.floor(Date.now() / 1000),
        chat: { id: Number(message.chat_id), type: "private" },
        text: message.text,
      },
      data,
    },
  };
  try {
    const r = await fetch(forwardUrl, {
      method: "POST",
      headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": hook.secret },
      body: JSON.stringify(update),
      signal: AbortSignal.timeout(10000),
    });
    return { status: 200, body: { ok: r.ok, webhook_status: r.status, update_id: updateId } };
  } catch (e) {
    return { status: 502, body: { ok: false, error: `webhook unreachable: ${e?.name ?? "error"}` } };
  }
}

const PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Approved demo: approver chat</title>
<style>
:root{--bg:#eef1f5;--fg:#17202a;--muted:#5b6673;--bubble:#fff;--judge:#fff8e6;--judge-edge:#e0b84f;--btn:#2f6fdf;--btn-no:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#10151b;--fg:#e6ebf1;--muted:#98a4b2;--bubble:#1b232d;--judge:#2a2414;--judge-edge:#a8862d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{padding:14px 16px;border-bottom:1px solid #8883}header h1{margin:0;font-size:17px}header p{margin:4px 0 0;color:var(--muted);font-size:13px}
main{max-width:720px;margin:0 auto;padding:12px 16px 40px}
.msg{background:var(--bubble);border-radius:12px;padding:10px 12px;margin:10px 0;box-shadow:0 1px 2px #0002;white-space:pre-wrap;word-wrap:break-word}
.msg.judge{background:var(--judge);border-left:4px solid var(--judge-edge)}
.meta{color:var(--muted);font-size:12px;margin-bottom:4px}
.btns{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
button{border:0;border-radius:8px;padding:8px 14px;font:inherit;color:#fff;background:var(--btn);cursor:pointer}
button.no{background:var(--btn-no)}button:disabled{opacity:.5;cursor:default}
code,pre{font-family:ui-monospace,monospace;font-size:13px}
.empty{color:var(--muted);text-align:center;margin-top:40px}
</style></head><body>
<header><h1>Approver chat (fake Telegram)</h1><p>Approval prompts from the gate, and the AI judge's advisories. Buttons tap as the demo approver.</p></header>
<main id="chat"><p class="empty">Waiting for the first message&hellip;</p></main>
<script>
const ALLOWED = new Set(["B","STRONG","I","EM","U","S","CODE","PRE","A","BR"]);
function render(html, into){
  const doc = new DOMParser().parseFromString("<div>"+html+"</div>", "text/html");
  const walk = (node, out) => {
    for (const child of node.childNodes){
      if (child.nodeType === 3){ out.appendChild(document.createTextNode(child.textContent)); continue; }
      if (child.nodeType !== 1) continue;
      if (ALLOWED.has(child.tagName)){
        const el = document.createElement(child.tagName === "A" ? "span" : child.tagName);
        walk(child, el); out.appendChild(el);
      } else { walk(child, out); }
    }
  };
  walk(doc.body.firstChild, into);
}
let shown = "";
async function refresh(){
  let data;
  try { data = await (await fetch("/api/chat")).json(); } catch { return; }
  const key = JSON.stringify(data.messages);
  if (key === shown) return;
  shown = key;
  const chat = document.getElementById("chat");
  chat.textContent = "";
  if (!data.messages.length){ chat.innerHTML = '<p class="empty">Waiting for the first message&hellip;</p>'; return; }
  for (const m of data.messages){
    const div = document.createElement("div");
    const judge = m.text.includes("Judge (advisory AI");
    div.className = "msg" + (judge ? " judge" : "");
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = (judge ? "AI judge (advisory)" : "approval gate") + " \\u00b7 #" + m.message_id + (m.edited ? " \\u00b7 edited" : "");
    div.appendChild(meta);
    const body = document.createElement("div"); render(m.text, body); div.appendChild(body);
    if (m.buttons.length){
      const row = document.createElement("div"); row.className = "btns";
      for (const b of m.buttons){
        const btn = document.createElement("button");
        btn.textContent = b.text;
        if (/^r:|reject|deny/i.test(b.data + " " + b.text)) btn.className = "no";
        btn.onclick = async () => {
          for (const x of row.querySelectorAll("button")) x.disabled = true;
          await fetch("/api/tap", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({message_id: m.message_id, data: b.data})});
          setTimeout(refresh, 500);
        };
        row.appendChild(btn);
      }
      div.appendChild(row);
    }
    chat.appendChild(div);
  }
  window.scrollTo(0, document.body.scrollHeight);
}
refresh(); setInterval(refresh, 1000);
</script></body></html>`;

createServer(async (req, res) => {
  const path = (req.url ?? "").split("?")[0] ?? "";
  if (req.method === "GET" && path === "/") {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(PAGE);
    return;
  }
  if (req.method === "GET" && path === "/healthz") return json(res, 200, { ok: true });
  if (req.method === "GET" && path === "/api/chat") {
    const own = messages.filter((m) => m.chat_id === chatId);
    return json(res, 200, {
      messages: own,
      webhooks: Object.fromEntries(Object.entries(webhooks).map(([bot, w]) => [bot, { url: w.url }])),
      calls: calls.map((c) => c.method),
    });
  }
  if (req.method === "POST" && path === "/api/tap") {
    const body = await readBody(req);
    const result = await tap(Number(body.message_id), String(body.data ?? ""));
    return json(res, result.status, result.body);
  }

  const match = /^\/bot([^/]+)\/([A-Za-z]+)$/u.exec(path);
  if (match === null) return json(res, 404, { ok: false, error_code: 404, description: "Not Found" });
  const bot = botIdOf(decodeURIComponent(match[1]));
  const method = match[2];
  const body = await readBody(req);
  calls.push({ method, bot, at: new Date().toISOString() });
  const now = Math.floor(Date.now() / 1000);
  switch (method) {
    case "getMe":
      return json(res, 200, { ok: true, result: { id: Number(bot), is_bot: true, first_name: "Demo", username: `demo_${bot}_bot` } });
    case "getWebhookInfo":
      return json(res, 200, { ok: true, result: { url: webhooks[bot]?.url ?? "", has_custom_certificate: false, pending_update_count: 0 } });
    case "setWebhook":
      webhooks[bot] = {
        url: typeof body.url === "string" ? body.url : "",
        secret: typeof body.secret_token === "string" ? body.secret_token : "",
      };
      return json(res, 200, { ok: true, result: true, description: "Webhook was set" });
    case "deleteWebhook":
      webhooks[bot] = { url: "", secret: "" };
      return json(res, 200, { ok: true, result: true, description: "Webhook was deleted" });
    case "sendMessage": {
      messageId += 1;
      messages.push({
        bot,
        message_id: messageId,
        chat_id: String(body.chat_id),
        text: typeof body.text === "string" ? body.text : "",
        buttons: buttonsOf(body.reply_markup),
        edited: false,
        at: new Date().toISOString(),
      });
      return json(res, 200, { ok: true, result: { message_id: messageId, date: now, chat: { id: Number(body.chat_id), type: "private" }, text: body.text } });
    }
    case "editMessageText": {
      const m = messages.find((x) => x.message_id === Number(body.message_id));
      if (m) {
        if (typeof body.text === "string") m.text = body.text;
        m.buttons = buttonsOf(body.reply_markup);
        m.edited = true;
      }
      return json(res, 200, { ok: true, result: { message_id: Number(body.message_id), date: now, chat: { id: Number(body.chat_id), type: "private" }, text: body.text } });
    }
    case "editMessageReplyMarkup": {
      const m = messages.find((x) => x.message_id === Number(body.message_id));
      if (m) m.buttons = buttonsOf(body.reply_markup);
      return json(res, 200, { ok: true, result: true });
    }
    case "getUpdates":
      return json(res, 409, { ok: false, error_code: 409, description: "Conflict: can't use getUpdates method while webhook is active" });
    default:
      return json(res, 200, { ok: true, result: true });
  }
}).listen(port, "0.0.0.0", () => {
  process.stderr.write(`fake-telegram: listening on 0.0.0.0:${String(port)}\n`);
});

process.on("SIGTERM", () => process.exit(0));
