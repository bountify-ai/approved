# Captured facade answers

These JSON files are copied byte for byte from
`approval-md-hosted/tests/fixtures/monitor/` (see `capture.json` for the image, runtime commit
and capture time). They are real `/log/follow` answers from a throwaway container of the
approval.md daemon image: every record in them was appended by the runtime itself (policy
attest, `POST /hook/hermes`, and the daemon's TTL sweep). None was written by hand.

The judge's tests replay them unchanged through a fake facade to prove the record shapes it
parses are the shapes the runtime writes. Tests that need a longer or broken chain build one
in memory in `tests/fakes.py`, clearly as a fake facade, never as a tenant log.
