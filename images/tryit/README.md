# The try-it image

The private Approved demo backend in one container behind one public port. What it is, how to deploy and
embed it, its limits and its security model: [docs/tryit.md](../../docs/tryit.md).

| file | what |
|---|---|
| `Dockerfile` | the image; build context is the repository root (`docker build -f images/tryit/Dockerfile .`) |
| `Dockerfile.dockerignore` | the build context's allowlist, which BuildKit reads for this Dockerfile |
| `loopback.mjs` | a `node --import` preload that moves the daemon supervisor's and the fake Telegram's listen to 127.0.0.1 |
| `smoke.sh`, `smoke.py` | `make tryit-smoke`: the image, headless, through its public port only |

`make tryit` requires exported `TRYIT_GATEWAY_SECRET`, `WANDB_API_KEY`, and `REVIEWER_MODEL`.
It runs the backend only, so local requests must send `X-Approved-Gateway`. To exercise the
browser interaction through the backend without supplying paid credentials, use
`make tryit-smoke`, which uses an unreachable local model endpoint and fake key. `make demo`
remains the local offline comparison.

The supervisor and the front server are Python, in the service package
([`service/src/approved/tryit/`](../../service/src/approved/tryit/)), so `make lint typecheck test`
covers them.
