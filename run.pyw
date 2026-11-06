# -*- coding: utf-8 -*-
"""双击运行（无控制台）：用 .venv\\Scripts\\pythonw.exe run.pyw"""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

class _Tee:
    """日志始终写入 data/wisland.log；有控制台时同时输出到控制台。"""
    def __init__(self, *streams):
        self.streams = [x for x in streams if x is not None]

    def write(self, s):
        for x in self.streams:
            try:
                x.write(s)
            except Exception:
                pass

    def flush(self):
        for x in self.streams:
            try:
                x.flush()
            except Exception:
                pass


os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
_log = open(os.path.join(ROOT, "data", "wisland.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.stdout, _log)
sys.stderr = _Tee(sys.stderr, _log)

try:
    from wisland.app import main
    sys.exit(main())
except SystemExit:
    raise
except Exception:
    traceback.print_exc()
    raise
