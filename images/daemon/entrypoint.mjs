#!/usr/bin/env node
/**
 * The daemon image's entrypoint: one supervisor, two or three children, one
 * listener.
 *
 * ## What this program is for
 *
 * Maritime's custom-code contract (docs/02 section 4.3) wants a container that
 * binds an HTTP server on `0.0.0.0:$PORT`, answers `GET /health` with a fast
 * 2xx and `POST /chat` within 30 seconds, keeps its state under `/data`, and
 * is started as a real program rather than a shell string. The approval.md
 * runtime already ships the HTTP server the facade needs (`approval serve`,
 * core APRV-421), and it authenticates EVERY path before it routes: there is
 * deliberately no unauthenticated surface on it, "not a health check and not a
 * 404" in its own words. So `/health` and `/chat` cannot come from it.
 *
 * This program therefore owns the injected port and nothing else owns it:
 *
 * - `GET /health` and `POST /chat` are answered here, from process state, with
 *   no store read and no side effect.
 * - `GET /schedules` is answered here too, from a constant built at startup:
 *   the one wake schedule Maritime should register for this machine (the
 *   external tick, see `readTickSchedule`).
 * - Every other path is reverse-proxied, unchanged, to `approval serve` on the
 *   loopback interface. Method, path, query, request body, `Authorization`,
 *   status, response headers and response bytes all pass through untouched, so
 *   the credential split, the refusal vocabulary and the harness dialects stay
 *   the core runtime's. This file authenticates nothing and decides nothing.
 * - `approval up` (the daemon loop and the configured channels) runs beside it
 *   under the same supervisor, so one wake resumes both.
 * - In webhook mode (`APPROVAL_TG_WEBHOOK_SECRET` set), a third child runs
 *   `approval channel telegram webhook` on loopback, `approval up` runs with
 *   `--no-telegram` so exactly one process owns the bot, and the one path
 *   `/telegram/webhook` is proxied to that child instead of to `serve`. That
 *   path needs no credential from this process: Telegram's secret header is the
 *   authentication, and the verb checks it before anything else.
 *
 * ## Why `approval serve` binds loopback and not `$PORT`
 *
 * Because this process holds `$PORT`, `serve` never needs a routable bind, and
 * a loopback bind is the configuration its own startup check prefers:
 * `--allow-non-loopback` exists to make an operator say out loud that bearer
 * credentials are crossing a cleartext hop. Inside this image they do not. The
 * external hop is Maritime's, terminated in front of the machine.
 *
 * ## What this program never does
 *
 * It never reads, prints, forwards to a log, or defaults a credential. The two
 * serve credentials, the bot token, the chat id, the webhook secret and
 * `APPROVAL_DAEMON_ID` are read by the children straight out of the container
 * environment, which is where the hosted deployment puts them (core SPEC 11.1
 * invariant 7: configuration is never loaded implicitly from the working
 * tree). The one exception is a presence test: whether
 * `APPROVAL_TG_WEBHOOK_SECRET` is non-empty selects webhook mode, and its value
 * goes nowhere but the child's inherited environment. The bot token's and the
 * chat id's variable NAMES are the tenant policy's (`channels.telegram.token_env`
 * and `chat_id_env`), so this file assumes no name for either and asks the
 * verbs rather than the environment whether a channel is configured. It never writes to
 * `events.jsonl`, never repairs a store, and never scaffolds over one that
 * already exists.
 */

import { spawn, spawnSync } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import net from "node:net";
import path from "node:path";

/** Where `approval serve` listens, reachable only from inside the container. */
const DEFAULT_INTERNAL_PORT = 4682;

/** The second choice, used when the injected port collides with the first. */
const ALTERNATE_INTERNAL_PORT = 4683;

/**
 * Where `approval channel telegram webhook` listens in webhook mode: the verb's
 * own default, moved up only when the injected port or `serve`'s internal port
 * already holds it.
 */
const WEBHOOK_INTERNAL_PORTS = [4683, 4684, 4685];

/**
 * The one path the supervisor sends to the webhook child, as it arrives at this
 * process. Maritime's public-URL proxy removes its `/a/<agent-id>` prefix before
 * a request reaches the machine, so this is the tail of the registered URL, and
 * the child is asked on the registered URL's own path (see `readWebhookMode`).
 */
const WEBHOOK_FRONT_PATH = "/telegram/webhook";

/** The CLI this image built, addressed as a file so no PATH or shebang is load-bearing. */
const DEFAULT_CLI = "/opt/runtime/node_modules/approval-md/cli.js";

/** The volume Maritime keeps across sleep, restart and redeploy. */
const DEFAULT_DATA_DIR = "/data";

/**
 * How long after `serve` is spawned `/health` still answers 2xx on the strength
 * of the child being alive alone.
 *
 * The contract asks for a fast 2xx at boot, and a Node process that has not yet
 * finished binding is not a fault. After the window a bound listener is
 * required, so a `serve` that starts and never listens reads unhealthy.
 */
const BIND_GRACE_MS = 15_000;

/** Restart backoff for a child that exits: doubling, from a beat to half a minute. */
const RESTART_MIN_MS = 500;
const RESTART_MAX_MS = 30_000;

/** How long a child gets to honour SIGTERM before it is killed. */
const SHUTDOWN_GRACE_MS = 20_000;

/**
 * The alternate carrier for the facade credential.
 *
 * Observed 2026-09-22 on the first Maritime deploy (HOSTED-1): the public-URL
 * proxy in front of a machine strips `Authorization` before the request reaches
 * the container, so every external call with a valid credential was refused as
 * if none had been sent, while the same call on loopback succeeded. A client
 * behind such a proxy sends the same `Bearer <credential>` value in this header
 * instead. The supervisor moves it to `Authorization` on the loopback hop ONLY
 * when no `Authorization` header arrived; when both arrive, `Authorization`
 * wins and the alternate is dropped, so a caller cannot smuggle a second
 * credential past one the proxy did forward. `approval serve` itself is
 * unchanged and still sees exactly one `Authorization` header, or none.
 */
const ALT_AUTHORIZATION = "x-approval-authorization";

/** The external tick's cron when `APPROVAL_TICK_CRON` is unset: every 15 minutes. */
const DEFAULT_TICK_CRON = "*/15 * * * *";

/** The id of the one entry `GET /schedules` serves. Fixed, so Maritime sees one trigger across redeploys. */
const TICK_SCHEDULE_ID = "approval-tick";

/**
 * The inclusive range of each cron field, in order: minute, hour, day of month,
 * month, day of week (0 and 7 are both Sunday). Names (`MON`, `JAN`) are not
 * accepted: the variable's character set is digits and `* , / -` only.
 */
const CRON_FIELDS = [
  { name: "minute", min: 0, max: 59 },
  { name: "hour", min: 0, max: 23 },
  { name: "day of month", min: 1, max: 31 },
  { name: "month", min: 1, max: 12 },
  { name: "day of week", min: 0, max: 7 },
];

/**
 * The most of a `POST /chat` body this process holds in memory. The platform's
 * scheduled delivery is a prompt of a few words; a body past this answers 413
 * and is never parsed.
 */
const CHAT_BODY_LIMIT = 64 * 1024;

/** The fixed answer to the platform's scheduled delivery. Names nothing about the tenant. */
const TICK_REPLY = "tick received; the daemon's sweep runs on its own schedule";

/** Hop-by-hop headers, which belong to one connection and are not forwarded. */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

const started = Date.now();

function log(message) {
  // stderr, always: stdout belongs to whatever a child decides to print there.
  process.stderr.write(`approval-image: ${message}\n`);
}

function fatal(message) {
  log(message);
  process.exit(2);
}

/**
 * The port this process binds, or a refusal.
 *
 * There is no default. Maritime injects `PORT` (its custom-code page says 18789
 * today, and `maritime create --port` defaults to 8080 for a public web app),
 * so a number compiled in here would be a number that is right until the day
 * the platform changes it, and the failure then is a machine that boots, looks
 * healthy to itself and is reachable by nobody.
 */
function readPort() {
  const raw = (process.env["PORT"] ?? "").trim();
  if (raw.length === 0) {
    fatal(
      "PORT is unset. The platform injects it and this image binds exactly what it is given; pass -e PORT=<n> when running the image by hand.",
    );
  }
  if (!/^\d+$/u.test(raw)) fatal(`PORT=${JSON.stringify(raw)} is not a port number`);
  const port = Number(raw);
  if (port < 1 || port > 65535) fatal(`PORT=${raw} is outside the TCP port range`);
  return port;
}

/**
 * The tenant whose store this machine runs, or a refusal.
 *
 * Unknown tenant refuses, per the fail-closed rule: a machine that guessed a
 * store directory would be a machine writing one tenant's decisions into
 * another tenant's log. The container exits and says why rather than starting
 * a gate over a directory nobody named.
 */
function readTenant() {
  const raw = (process.env["APPROVAL_TENANT"] ?? process.env["TENANT_ID"] ?? "").trim();
  if (raw.length === 0) {
    fatal(
      "no tenant id: set APPROVAL_TENANT (or TENANT_ID) in the machine environment. This image runs one tenant's store at <data dir>/<tenant> and will not guess which one.",
    );
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u.test(raw)) {
    fatal(
      `tenant id ${JSON.stringify(raw)} is not usable as a directory name: letters, digits, '.', '-' and '_', starting with a letter or digit, at most 64 characters`,
    );
  }
  return raw;
}

/**
 * The identity `approval serve` acts as, or a refusal.
 *
 * `serve` runs as ONE agent and records every tool call under that identity, so
 * it refuses to start without one, exactly as `approval mcp serve` does. This
 * process will not invent the name: a record carrying an identity nobody chose
 * is a record an operator cannot trace back to the harness that made it. It is
 * ordinary configuration rather than a credential, so it is checked here, up
 * front, and named in the machine environment beside the tenant id.
 */
function readAgentIdentity() {
  const raw = (process.env["APPROVAL_SERVE_AS"] ?? process.env["APPROVAL_AGENT"] ?? "").trim();
  if (raw.length === 0) {
    fatal(
      "no agent identity: set APPROVAL_SERVE_AS (or APPROVAL_AGENT) to agent:<id> in the machine environment. The facade acts as one agent and records every call under it; this image will not name that agent for you.",
    );
  }
  if (!raw.startsWith("agent:")) {
    fatal(
      `agent identity ${JSON.stringify(raw)} is not an agent: the facade is the party UNDER oversight and may not act as a human or as the system. Use agent:<id>.`,
    );
  }
  return raw;
}

function readInternalPort(externalPort) {
  const raw = (process.env["APPROVAL_SERVE_INTERNAL_PORT"] ?? "").trim();
  if (raw.length > 0) {
    if (!/^\d+$/u.test(raw)) fatal(`APPROVAL_SERVE_INTERNAL_PORT=${JSON.stringify(raw)} is not a port number`);
    const port = Number(raw);
    if (port < 1 || port > 65535) fatal(`APPROVAL_SERVE_INTERNAL_PORT=${raw} is outside the TCP port range`);
    if (port === externalPort) {
      fatal(
        `APPROVAL_SERVE_INTERNAL_PORT=${raw} is the injected PORT. This process holds that port and proxies to serve on another one.`,
      );
    }
    return port;
  }
  return externalPort === DEFAULT_INTERNAL_PORT ? ALTERNATE_INTERNAL_PORT : DEFAULT_INTERNAL_PORT;
}

/**
 * Webhook mode, or `null` for today's behaviour (`approval up` owns Telegram if
 * the tenant configured it).
 *
 * `APPROVAL_TG_WEBHOOK_SECRET` is the switch because it is the one thing the
 * webhook verb cannot run without, and the one fixed name in the channel's
 * configuration (core APRV-424 reads it by that name; the bot token and chat id
 * are named by the tenant's policy). Only its presence is tested here.
 *
 * `APPROVAL_PUBLIC_URL` is the machine's public base URL, set at provisioning
 * (on Maritime `https://api.maritime.sh/a/<agent-id>`). It is not a secret, but
 * this function still never echoes it: a URL someone typed can carry userinfo.
 * The registered webhook is `<public url>/telegram/webhook`, and the verb serves
 * exactly that URL's path (it refuses a `--path` that differs), so the loopback
 * hop is asked on that path while the front door answers `/telegram/webhook`,
 * which is what the platform's proxy delivers after stripping its prefix. When
 * the public URL has no path of its own the two are the same string.
 *
 * A secret without a usable public URL refuses at startup: the operator asked
 * for webhook mode, and neither guessing a URL nor quietly falling back to long
 * polling is what they asked for. HTTPS, the port and userinfo are the verb's to
 * refuse, in its own vocabulary, and it does.
 */
function readWebhookMode(externalPort, serveInternalPort) {
  const secret = process.env["APPROVAL_TG_WEBHOOK_SECRET"];
  if (secret === undefined || secret.trim().length === 0) return null;
  const raw = (process.env["APPROVAL_PUBLIC_URL"] ?? "").trim();
  if (raw.length === 0) {
    fatal(
      "APPROVAL_TG_WEBHOOK_SECRET is set, which selects Telegram webhook mode, and APPROVAL_PUBLIC_URL is not. Set APPROVAL_PUBLIC_URL to this machine's public https base URL (on Maritime, https://api.maritime.sh/a/<agent-id>); the webhook is registered at <that>/telegram/webhook. Unset the secret to run Telegram by long poll instead.",
    );
  }
  let base;
  try {
    base = new URL(raw);
  } catch {
    fatal("APPROVAL_PUBLIC_URL is not a URL (its value is not repeated here). It is this machine's public https base URL.");
  }
  if (base.search !== "" || base.hash !== "") {
    fatal(
      "APPROVAL_PUBLIC_URL carries a query or a fragment (its value is not repeated here). It is a base URL: /telegram/webhook is appended to its path.",
    );
  }
  const basePath = base.pathname.replace(/\/+$/u, "");
  base.pathname = `${basePath}${WEBHOOK_FRONT_PATH}`;
  const configured = (process.env["APPROVAL_WEBHOOK_INTERNAL_PORT"] ?? "").trim();
  let port;
  if (configured !== "") {
    if (!/^\d+$/u.test(configured) || Number(configured) < 1 || Number(configured) > 65535) {
      fatal("APPROVAL_WEBHOOK_INTERNAL_PORT is not a valid TCP port");
    }
    port = Number(configured);
    if (port === externalPort || port === serveInternalPort) {
      fatal("APPROVAL_WEBHOOK_INTERNAL_PORT collides with the daemon or facade listener");
    }
  } else {
    port = WEBHOOK_INTERNAL_PORTS.find((candidate) => candidate !== externalPort && candidate !== serveInternalPort);
  }
  return { url: base.href, upstreamPath: base.pathname, port };
}

/**
 * Whether one comma-separated item of one cron field is well formed and in
 * range: `*`, `n` or `n-m`, the first and last optionally followed by `/step`.
 * `n/step` is refused: cron dialects disagree on what it means, and a schedule
 * the platform reads differently from the operator is a tick at the wrong time.
 */
function cronItemIsValid(item, field) {
  const match = /^(\*|(\d{1,2})(?:-(\d{1,2}))?)(?:\/(\d{1,2}))?$/u.exec(item);
  if (match === null) return false;
  const [, base, low, high, step] = match;
  if (step !== undefined) {
    const every = Number(step);
    if (every < 1 || every > field.max) return false;
    if (base !== "*" && high === undefined) return false;
  }
  if (base === "*") return true;
  const from = Number(low);
  const to = high === undefined ? from : Number(high);
  return from >= field.min && to <= field.max && from <= to;
}

/**
 * The external tick: the wake schedule `GET /schedules` serves, or `null` when
 * the operator turned it off.
 *
 * A sleeping Maritime machine runs no timer, so the daemon's own TTL sweep
 * (every 30 s in `approval up`) stops with it, and a request whose window
 * lapses while the machine sleeps is recorded `approval.expired` only on the
 * next wake. The platform is the alarm clock: it polls `GET /schedules` while
 * the machine is awake, registers each entry as a real wake trigger, wakes the
 * VM shortly before each occurrence and delivers the entry's prompt to
 * `POST /chat` with `source: "scheduled"`. Waking is the whole point; the
 * daemon's loop does the rest (see the `/chat` handler).
 *
 * `APPROVAL_TICK_CRON` is a five-field cron in UTC: unset or empty means
 * `*\/15 * * * *`, the literal `off` means no entry, and anything else must be
 * five whitespace-separated fields of digits and `* , / -` with every value
 * inside its field's range. A bad value refuses at startup, exit 2, and the
 * message names the variable and never repeats the value: a value that is not a
 * cron is a value nobody should see echoed into a platform log. Range checking
 * goes one step past the character set because a schedule the platform cannot
 * register is a tick that silently never happens.
 */
function readTickSchedule() {
  const raw = (process.env["APPROVAL_TICK_CRON"] ?? "").trim();
  if (raw === "off") return null;
  const text = raw.length === 0 ? DEFAULT_TICK_CRON : raw;
  const fields = text.split(/\s+/u);
  const refuse = (why) =>
    fatal(
      `APPROVAL_TICK_CRON is not a usable schedule: ${why} (its value is not repeated here). It is a five-field cron in UTC made of digits and * , / - (for example */15 * * * *), or the word off to serve no wake schedule; unset, it defaults to ${DEFAULT_TICK_CRON}.`,
    );
  if (fields.length !== CRON_FIELDS.length) refuse(`it does not have exactly ${String(CRON_FIELDS.length)} fields`);
  if (!fields.every((field) => /^[0-9*,/-]+$/u.test(field))) {
    refuse("a field carries a character other than digits and * , / -");
  }
  CRON_FIELDS.forEach((field, index) => {
    if (!fields[index].split(",").every((item) => cronItemIsValid(item, field))) {
      refuse(`the ${field.name} field is not a list of *, n or n-m (optionally /step) inside ${String(field.min)}-${String(field.max)}`);
    }
  });
  return { cron: fields.join(" ") };
}

/**
 * Make sure the tenant's store directory exists, and scaffold it once when it
 * is empty.
 *
 * `approval quickstart` is the ceremony that produces a working policy, and it
 * is interactive only: piped stdin and `--json` exit 2 by design, because the
 * human reads the generated bytes and types `understood`. A container has no
 * human, so first boot runs `approval init`, which scaffolds the canonical
 * example policy, the log directory, the queue projection and the ignore
 * entries, and attests NOTHING. That is the correct end state for an
 * unattended boot: an unattested policy is inoperative, every class gates to a
 * human, and `approval doctor` names the missing attestation. A real tenant's
 * store arrives attested, by a human or by a copy of an existing store.
 *
 * An existing store is never touched: `init` is not even invoked when one is
 * present, and `init` itself never overwrites.
 */
function ensureStore(cli, store) {
  mkdirSync(store, { recursive: true });
  const scaffolded =
    existsSync(path.join(store, "APPROVAL.md")) ||
    existsSync(path.join(store, "APPROVALS.md")) ||
    existsSync(path.join(store, ".approval"));
  if (scaffolded) {
    log(`store ${store} already exists; leaving it exactly as it is`);
    return;
  }
  const entries = readdirSync(store);
  log(
    `store ${store} is ${entries.length === 0 ? "empty" : "unscaffolded"}; running 'approval init' once. The policy it writes is NOT attested, so every class gates to a human until one attests it.`,
  );
  // The daemon's default envelope folder, created ONCE beside the scaffold. An
  // absent one is only a warning (the TTL sweep and the queue projection run
  // regardless), and creating it here keeps a first boot quiet and gives a
  // tenant somewhere to put task envelopes.
  mkdirSync(path.join(store, "backlog", "tasks"), { recursive: true });
  const result = spawnSync(process.execPath, [cli, "init", "--dir", store, "--json"], {
    stdio: ["ignore", "inherit", "inherit"],
  });
  if (result.status !== 0) {
    // Not fatal, and not a widening: a store without a loadable policy makes
    // every class manual in the runtime itself. Starting lets an operator reach
    // /status and /verb/doctor to see what is wrong, which a container that
    // exited would not.
    log(
      `'approval init' exited ${String(result.status)}; starting anyway. Ask the facade for status and doctor: an unusable policy gates everything to a human.`,
    );
  }
}

/** One supervised child process. */
class Child {
  constructor(name, cli, args, cwd, hint) {
    this.name = name;
    this.hint = hint;
    this.cli = cli;
    this.args = args;
    this.cwd = cwd;
    this.proc = null;
    this.startedAt = 0;
    this.backoff = RESTART_MIN_MS;
    this.stopping = false;
    this.timer = null;
  }

  get alive() {
    return this.proc !== null && this.proc.exitCode === null && this.proc.signalCode === null;
  }

  start() {
    if (this.stopping) return;
    this.startedAt = Date.now();
    // The environment is passed through whole and untouched. The credentials,
    // the bot token and the daemon id are the child's to read; this process
    // neither inspects nor rewrites them.
    this.proc = spawn(process.execPath, [this.cli, ...this.args], {
      cwd: this.cwd,
      env: process.env,
      stdio: ["ignore", "inherit", "inherit"],
    });
    this.proc.on("exit", (code, signal) => this.onExit(code, signal));
    this.proc.on("error", (cause) => {
      log(`${this.name} could not be spawned: ${cause instanceof Error ? cause.message : String(cause)}`);
    });
    log(`${this.name} started, pid ${String(this.proc.pid ?? 0)}`);
  }

  onExit(code, signal) {
    if (this.stopping) return;
    const ran = Date.now() - this.startedAt;
    log(`${this.name} exited ${signal === null ? `code ${String(code)}` : `on ${signal}`} after ${String(ran)}ms`);
    if (code !== null && code !== 0 && ran < 5_000) {
      // The verbs refuse at startup, before they bind or poll anything: a
      // missing or short credential, a bad daemon id, a bad identity, a webhook
      // the Bot API would not take. Restarting will not fix it, so say what to
      // look at. The child's own line above this one names the refusal code.
      log(`${this.name} refused at startup (exit ${String(code)}). ${this.hint}`);
    }
    if (ran > 60_000) this.backoff = RESTART_MIN_MS;
    const wait = this.backoff;
    this.backoff = Math.min(this.backoff * 2, RESTART_MAX_MS);
    this.timer = setTimeout(() => this.start(), wait);
    if (typeof this.timer.unref === "function") this.timer.unref();
  }

  stop() {
    this.stopping = true;
    if (this.timer !== null) clearTimeout(this.timer);
    if (this.alive && this.proc !== null) this.proc.kill("SIGTERM");
  }

  kill() {
    if (this.alive && this.proc !== null) this.proc.kill("SIGKILL");
  }
}

/**
 * `--api-base <url>` when `APPROVAL_IMAGE_TG_API_BASE` is set, for whichever
 * child owns Telegram. A TEST SEAM: the smoke script points the Bot API at a
 * fake with it. The CLI has the flag and no environment variable, so the image
 * maps one to the other. A production machine leaves it unset, and the channel
 * talks to api.telegram.org.
 */
function telegramApiBase() {
  const raw = (process.env["APPROVAL_IMAGE_TG_API_BASE"] ?? "").trim();
  return raw.length === 0 ? [] : ["--api-base", raw];
}

/**
 * Forward one request to a loopback child and stream the answer back.
 *
 * The request body and the response bytes are piped, never parsed; hop-by-hop
 * headers are dropped in both directions. `unreachable` answers when nothing
 * is listening, and only while no header has been sent.
 */
function proxy(req, res, port, target, headers, unreachable) {
  const upstream = httpRequest(
    { host: "127.0.0.1", port, method: req.method, path: target, headers },
    (answer) => {
      const out = {};
      for (const [name, value] of Object.entries(answer.headers)) {
        if (HOP_BY_HOP.has(name.toLowerCase())) continue;
        out[name] = value;
      }
      res.writeHead(answer.statusCode ?? 502, out);
      answer.pipe(res);
    },
  );
  upstream.on("error", (cause) => {
    if (res.headersSent) {
      res.destroy();
      return;
    }
    unreachable(cause);
  });
  req.pipe(upstream);
}

function main() {
  const port = readPort();
  const tenant = readTenant();
  const agentIdentity = readAgentIdentity();
  const internalPort = readInternalPort(port);
  const cli = process.env["APPROVAL_CLI"] ?? DEFAULT_CLI;
  const dataDir = process.env["APPROVAL_DATA_DIR"] ?? DEFAULT_DATA_DIR;
  const store = path.join(dataDir, tenant);
  const webhook = readWebhookMode(port, internalPort);
  const tick = readTickSchedule();
  // Built once and never changed: no state, no secret, no tenant data. The same
  // bytes answer every poll, so a trigger the platform registered stays the one
  // this machine was started with.
  const schedules =
    tick === null ? [] : [{ id: TICK_SCHEDULE_ID, cron: tick.cron, tz: "UTC", prompt: "tick", enabled: true }];

  if (!existsSync(cli)) fatal(`the approval.md CLI is not at ${cli}; set APPROVAL_CLI to where it is`);

  ensureStore(cli, store);

  const daemon = new Child(
    "approval up",
    cli,
    [
      "up",
      "--dir",
      store,
      // The git preflight (fetch, fast-forward, rebuild) is a developer
      // checkout's workflow. A tenant store is not a checkout of this runtime,
      // and a container must not rebuild itself.
      "--no-preflight",
      // One listener in this container, and it is this process's. The queue
      // page is reachable through the facade's verbs instead.
      "--no-web",
      // Webhook mode: the webhook verb owns the bot, and a poller in the same
      // gate would be refused by the transport lease anyway. Saying so here
      // keeps `up` from asking the Bot API at all.
      ...(webhook === null ? telegramApiBase() : ["--no-telegram"]),
    ],
    store,
    "Check the machine environment: APPROVAL_DAEMON_ID must be a usable daemon id when it is set at all, and the bot token and chat id must be in the variables the tenant policy's channels.telegram names.",
  );
  const serve = new Child(
    "approval serve",
    cli,
    [
      "serve",
      "--dir",
      store,
      "--listen",
      `127.0.0.1:${String(internalPort)}`,
      ...(process.env["APPROVAL_SERVE_HOOK_TIMEOUT"] === undefined
        ? []
        : ["--hook-timeout", process.env["APPROVAL_SERVE_HOOK_TIMEOUT"]]),
      // APRV-423. Passed only when set: a Hermes tenant sets 300s, and without
      // it the runtime assumes Hermes's 30-second default and denies every
      // manual class hook-harness-cap-too-short, which is the fail-closed
      // reading of a ceiling nobody stated. The image does not state it for
      // the tenant.
      ...(process.env["APPROVAL_SERVE_HOOK_HARNESS_CAP"] === undefined
        ? []
        : ["--hook-harness-cap", process.env["APPROVAL_SERVE_HOOK_HARNESS_CAP"]]),
      "--as",
      agentIdentity,
    ],
    store,
    "Check the machine environment: APPROVAL_SERVE_AGENT_TOKEN and APPROVAL_SERVE_TENANT_TOKEN must both be set, distinct, and at least 24 characters; APPROVAL_DAEMON_ID must be a usable daemon id when it is set at all; APPROVAL_SERVE_HOOK_TIMEOUT and APPROVAL_SERVE_HOOK_HARNESS_CAP must be durations like 20s or 300s.",
  );
  const webhookChild =
    webhook === null
      ? null
      : new Child(
          "approval channel telegram webhook",
          cli,
          [
            "channel",
            "telegram",
            "webhook",
            "--dir",
            store,
            "--url",
            webhook.url,
            "--path",
            webhook.upstreamPath,
            "--listen",
            `127.0.0.1:${String(webhook.port)}`,
            ...telegramApiBase(),
          ],
          store,
          "Check the machine environment: APPROVAL_TG_WEBHOOK_SECRET must be 24 to 256 characters of A-Z a-z 0-9 _ -, APPROVAL_PUBLIC_URL must be https on 443, 80, 88 or 8443, APPROVAL_HUMAN must be set, the bot token and chat id must be in the variables the tenant policy's channels.telegram names, and the Bot API must be reachable. A webhook this bot already has registered elsewhere is refused until an operator reclaims it.",
        );
  const children = [daemon, serve, ...(webhookChild === null ? [] : [webhookChild])];

  let bound = false;
  const probe = setInterval(() => {
    if (!serve.alive) {
      bound = false;
      return;
    }
    if (bound) return;
    const socket = net.connect({ host: "127.0.0.1", port: internalPort });
    socket.setTimeout(1_000);
    socket.on("connect", () => {
      bound = true;
      socket.destroy();
    });
    const give_up = () => socket.destroy();
    socket.on("timeout", give_up);
    socket.on("error", give_up);
  }, 250);
  if (typeof probe.unref === "function") probe.unref();

  const healthy = () =>
    daemon.alive &&
    serve.alive &&
    (webhookChild === null || webhookChild.alive) &&
    (bound || Date.now() - serve.startedAt < BIND_GRACE_MS);

  const json = (res, status, body) => {
    const bytes = Buffer.from(`${JSON.stringify(body)}\n`, "utf8");
    res.writeHead(status, { "content-type": "application/json", "content-length": bytes.length });
    res.end(bytes);
  };

  const front = createServer((req, res) => {
    let pathname = req.url ?? "/";
    const query = pathname.indexOf("?");
    if (query >= 0) pathname = pathname.slice(0, query);

    // GET /health: process state only. No store read, no log read, no child
    // touched, nothing appended. Maritime polls this and a health check with a
    // side effect is a side effect on a schedule.
    if (pathname === "/health") {
      if (req.method !== "GET" && req.method !== "HEAD") {
        json(res, 405, { status: "error", code: "image-method-not-allowed", message: "/health answers GET" });
        return;
      }
      const ok = healthy();
      json(res, ok ? 200 : 503, {
        status: ok ? "ok" : "unhealthy",
        daemon: daemon.alive ? "running" : "down",
        facade: serve.alive ? (bound ? "listening" : "starting") : "down",
        ...(webhookChild === null ? {} : { webhook: webhookChild.alive ? "running" : "down" }),
        uptime_s: Math.round((Date.now() - started) / 1000),
      });
      return;
    }

    // GET /schedules: the external tick, for the platform to register as a wake
    // trigger (see readTickSchedule). A constant: no credential is asked for,
    // because there is nothing here a credential would protect, and nothing is
    // read, touched or appended.
    if (pathname === "/schedules") {
      if (req.method !== "GET" && req.method !== "HEAD") {
        json(res, 405, { status: "error", code: "image-method-not-allowed", message: "/schedules answers GET" });
        return;
      }
      json(res, 200, schedules);
      return;
    }

    // POST /chat: the contract's conversational endpoint, answered with a fixed
    // refusal. This machine is a gate, not an assistant, and it holds one
    // tenant's decisions. The body names nothing about the tenant, because this
    // endpoint carries no credential and so may disclose nothing.
    //
    // The one body this process looks inside is the platform's scheduled
    // delivery of the external tick (`"source": "scheduled"`), and it answers
    // that with a fixed acknowledgement so the delivery counts as delivered.
    // It TRIGGERS NOTHING, and that is deliberate: the wake is the tick. Once
    // the machine is awake, `approval up`'s own loop runs its sweep on its own
    // 30-second timer (core daemon.ts, a setInterval that resumes with the
    // snapshot), expiring lapsed requests as `system:gate` within one interval,
    // and the machine then stays awake for the platform's idle TTL. A trigger
    // path here would be an unauthenticated caller choosing when the gate
    // writes, and anyone can post this body.
    if (pathname === "/chat") {
      if (req.method !== "POST") {
        json(res, 405, { status: "error", code: "image-method-not-allowed", message: "/chat answers POST" });
        return;
      }
      const chunks = [];
      let held = 0;
      let oversized = false;
      req.on("data", (chunk) => {
        if (oversized) return;
        held += chunk.length;
        if (held > CHAT_BODY_LIMIT) {
          // Keep draining so the answer can be written, but stop holding bytes.
          oversized = true;
          chunks.length = 0;
          return;
        }
        chunks.push(chunk);
      });
      req.on("end", () => {
        if (oversized) {
          json(res, 413, {
            status: "error",
            code: "image-chat-too-large",
            message: `/chat reads at most ${String(CHAT_BODY_LIMIT)} bytes`,
          });
          return;
        }
        let source;
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (body !== null && typeof body === "object" && !Array.isArray(body)) source = body.source;
        } catch {
          // Not JSON: an ordinary chat message, answered with the refusal below.
        }
        if (source === "scheduled") {
          json(res, 200, { ok: true, code: "tick-received", reply: TICK_REPLY });
          return;
        }
        json(res, 200, {
          ok: false,
          code: "chat-unsupported",
          message:
            "This machine runs an approval.md gate, not a chat agent. It has no conversational surface. Approvals are decided by the tenant's approver through the tenant's own channel, and the machine's HTTP surface is the authenticated approval.md facade: /verbs, /verb/<name>, /hook/<harness> with the agent credential, and /log/follow, /export, /status with the tenant credential.",
          reply:
            "No conversation here. This is an approval gate: ask it through the facade with a credential, or ask the approver through their channel.",
        });
      });
      return;
    }

    // Webhook mode: Telegram's deliveries go to the webhook verb, with every
    // header as it arrived. No credential is required here and none is moved:
    // `X-Telegram-Bot-Api-Secret-Token` is the authentication, the verb checks
    // it before the path, the method or the body, and a post without it is
    // refused there and appends nothing. The alternate-carrier rewrite below
    // belongs to the facade's bearer credentials and does not apply.
    if (webhook !== null && pathname === WEBHOOK_FRONT_PATH) {
      const passthrough = {};
      for (const [name, value] of Object.entries(req.headers)) {
        const lower = name.toLowerCase();
        if (HOP_BY_HOP.has(lower)) continue;
        if (lower === "host") continue;
        passthrough[name] = value;
      }
      passthrough["host"] = `127.0.0.1:${String(webhook.port)}`;
      const suffix = query >= 0 ? (req.url ?? "").slice(query) : "";
      proxy(req, res, webhook.port, `${webhook.upstreamPath}${suffix}`, passthrough, () =>
        json(res, 503, {
          error: {
            code: "image-webhook-unavailable",
            message:
              "the Telegram webhook receiver is not answering inside this machine. Nothing was recorded; Telegram retries a delivery that did not get a 2xx.",
          },
        }),
      );
      return;
    }

    // Everything else is the core runtime's, unchanged.
    const headers = {};
    for (const [name, value] of Object.entries(req.headers)) {
      const lower = name.toLowerCase();
      if (HOP_BY_HOP.has(lower)) continue;
      if (lower === "host") continue;
      if (lower === ALT_AUTHORIZATION) continue;
      headers[name] = value;
    }
    if (req.headers.authorization === undefined && typeof req.headers[ALT_AUTHORIZATION] === "string") {
      headers["authorization"] = req.headers[ALT_AUTHORIZATION];
    }
    headers["host"] = `127.0.0.1:${String(internalPort)}`;

    proxy(req, res, internalPort, req.url ?? "/", headers, (cause) => {
      // A hook client reads a body with no block directive as an ALLOW on
      // several dialects, so the fail-closed answer for an unreachable facade
      // carries a non-zero `exit_code` and no verdict. A caller of /hook MUST
      // treat any non-200 from this facade as a block; this body is the reason
      // it can, and it deliberately does not invent a harness dialect, because
      // a second implementation of the gate's vocabulary would be a second gate.
      json(res, 503, {
        exit_code: 2,
        stdout: "",
        stderr: "approval serve is not reachable inside this machine",
        error: {
          code: "image-facade-unavailable",
          message: `approval serve is not answering on 127.0.0.1:${String(internalPort)}: ${
            cause instanceof Error ? cause.message : String(cause)
          }. Treat this as a refusal: nothing was classified, requested or granted.`,
        },
      });
    });
  });

  front.on("clientError", (_cause, socket) => {
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\nconnection: close\r\n\r\n");
  });

  front.listen(port, "0.0.0.0", () => {
    log(
      `listening on 0.0.0.0:${String(port)} for tenant ${tenant}, store ${store}. /health, /chat and /schedules are answered here; every other path is the approval.md facade on 127.0.0.1:${String(internalPort)}.`,
    );
    log(
      tick === null
        ? "external tick off (APPROVAL_TICK_CRON=off): /schedules serves no wake schedule, so a request that lapses while this machine sleeps is recorded expired only on the next wake."
        : `external tick: /schedules serves '${tick.cron}' (UTC) for the platform to wake this machine on; the daemon's own sweep runs once it is awake.`,
    );
  });
  front.on("error", (cause) => {
    fatal(`could not bind 0.0.0.0:${String(port)}: ${cause instanceof Error ? cause.message : String(cause)}`);
  });

  if (webhook !== null) {
    log(
      `Telegram webhook mode: 'approval channel telegram webhook' on 127.0.0.1:${String(webhook.port)} owns the bot, ${WEBHOOK_FRONT_PATH} is proxied to it with no credential rewrite, and 'approval up' runs with --no-telegram.`,
    );
  }

  for (const child of children) child.start();

  let stopping = false;
  const stop = (signal) => {
    if (stopping) return;
    stopping = true;
    log(`${signal} received; stopping ${children.map((child) => child.name).join(", ")}`);
    clearInterval(probe);
    front.close();
    for (const child of children) child.stop();
    const hard = setTimeout(() => {
      for (const child of children) child.kill();
      process.exit(0);
    }, SHUTDOWN_GRACE_MS);
    if (typeof hard.unref === "function") hard.unref();
    const wait = setInterval(() => {
      if (children.every((child) => !child.alive)) {
        clearInterval(wait);
        clearTimeout(hard);
        process.exit(0);
      }
    }, 100);
  };
  process.on("SIGTERM", () => stop("SIGTERM"));
  process.on("SIGINT", () => stop("SIGINT"));
}

main();
