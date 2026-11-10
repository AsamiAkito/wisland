# -*- coding: utf-8 -*-
"""双击运行（无控制台）：用 .venv\\Scripts\\pythonw.exe run.pyw"""
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))


def _relaunch_windowless():
    """uv 创建的 .venv 里 Scripts\\pythonw.exe 其实是控制台程序，直接用它启动（例如旧的开机自启项、
    电脑管家恢复的启动项）会弹出终端窗口，关掉窗口灵动岛也会退出。
    检测到自己挂在控制台上时，改用基础解释器的 pythonw.exe 重新启动自己并立即退出。"""
    if os.environ.get("WISLAND_CONSOLE") or os.environ.get("WISLAND_RELAUNCHED"):
        return
    try:
        import ctypes
        if not ctypes.windll.kernel32.GetConsoleWindow():
            return
        import subprocess
        base = os.path.dirname(getattr(sys, "_base_executable", "") or sys.executable)
        pyw = os.path.join(base, "pythonw.exe")
        if not os.path.isfile(pyw):
            return
        env = dict(os.environ, WISLAND_RELAUNCHED="1")
        venv_py = os.path.join(sys.prefix, "Scripts", "python.exe")
        if sys.prefix != sys.base_prefix and os.path.isfile(venv_py):
            env["__PYVENV_LAUNCHER__"] = venv_py
        subprocess.Popen([pyw, os.path.abspath(__file__)] + sys.argv[1:], cwd=ROOT, env=env,
                         creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | NEW_PROCESS_GROUP
                         close_fds=True)
    except Exception:
        return
    os._exit(0)


_relaunch_windowless()
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
