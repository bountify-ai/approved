/**
 * Loopback-only listening, for the try-it image's Node children.
 *
 * Loaded with `node --import file:///opt/tryit/loopback.mjs` in front of two programs this
 * image runs unchanged: the daemon image's supervisor (`images/daemon/entrypoint.mjs`, which
 * binds `0.0.0.0:$PORT`) and the demo's fake Telegram (`demo/fake-telegram/server.mjs`,
 * which binds `0.0.0.0:$FAKE_TG_PORT`). In the try-it container only the front server may
 * listen on a routable address, so every TCP listen in these two processes is moved to
 * 127.0.0.1 instead of forking either file.
 *
 * `--import` applies to the one process it is given to: the daemon supervisor's own children
 * (`approval up`, `approval serve`, the webhook verb) are spawned without it, and they bind
 * loopback by their own arguments already. A listen this file cannot read (a pipe path, a
 * handle) is left alone; the image smoke checks /proc/net/tcp for anything routable.
 */
import net from "node:net";

const LOOPBACK = "127.0.0.1";
const original = net.Server.prototype.listen;

const isPort = (value) =>
  typeof value === "number" || (typeof value === "string" && /^\d+$/u.test(value));

net.Server.prototype.listen = function listenOnLoopback(...args) {
  const [first] = args;
  if (first !== null && typeof first === "object" && !Array.isArray(first) && "port" in first) {
    args[0] = { ...first, host: LOOPBACK };
  } else if (isPort(first)) {
    if (typeof args[1] === "string") args[1] = LOOPBACK;
    else args.splice(1, 0, LOOPBACK);
  }
  return original.apply(this, args);
};
