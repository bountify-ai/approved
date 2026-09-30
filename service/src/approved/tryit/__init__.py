"""Approved, try it: the local demo behind one public port, in one container.

``python -m approved.tryit`` is the image command of ``images/tryit/Dockerfile``. It supervises
the demo's parts on loopback (the approval.md daemon image's own entrypoint, the fake Telegram,
the judge with its console off) and serves the only public listener: a page where a visitor
runs the scripted agent and answers its requests. See ``docs/tryit.md``.
"""
