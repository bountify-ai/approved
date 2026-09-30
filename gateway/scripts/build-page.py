"""Generate the Vercel shell from the runtime's single page source."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
source = runpy.run_path(str(ROOT / 'service/src/approved/tryit/page.py'))
expected = source['render_page']('')
target = ROOT / 'gateway/page.html'
if '--check' in sys.argv:
    if not target.exists() or target.read_text() != expected:
        raise SystemExit('gateway/page.html is stale; run python3 gateway/scripts/build-page.py')
    print('gateway/page.html matches page.py')
else:
    target.write_text(expected)
    print(target)
