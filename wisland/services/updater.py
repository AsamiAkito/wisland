# -*- coding: utf-8 -*-
"""源码自动更新服务：检查新版本、下载 CNB 压缩包并安全替换程序文件。

设计目标：
- 纯逻辑、不导入 PySide6，方便在后台线程和单元测试里直接调用；
- 只替换白名单里的程序文件，``config.json`` / ``data`` / ``models`` / ``.venv`` 等用户数据永远不碰；
- 替换前备份、失败时回滚，下载和解压都可以用 ``threading.Event`` 取消；
- 打包版（PyInstaller）不能替换正在运行的 exe，由界面引导用户打开开源仓库。

线上地址使用 CNB 的公开 raw / archive 接口，不依赖 CNB API（匿名请求会 401）。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .. import __version__ as APP_VERSION

# CNB 公开接口（已实测：不需要登录，也不用 CNB Releases）
VERSION_URL = "https://cnb.cool/shiroki.live/wisland-zus/-/git/raw/main/VERSION"
INIT_URL = "https://cnb.cool/shiroki.live/wisland-zus/-/git/raw/main/wisland/__init__.py"
ARCHIVE_URL = "https://cnb.cool/shiroki.live/wisland-zus/-/git/archive/main.zip"
REPO_URL = "https://cnb.cool/shiroki.live/wisland-zus"

USER_AGENT = f"wisland/{APP_VERSION} updater"

# 只更新这些路径（存在才处理）；顺序固定，目录先于文件
UPDATE_DIRS = ("wisland", "scripts", "docs")
UPDATE_FILES = (
    "VERSION",
    "run.pyw",
    "setup.bat",
    "wisland.vbs",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "config.example.json",
    ".gitattributes",
    ".gitignore",
)
REQUIRED_STAGE_FILES = (
    "wisland/__init__.py",
    "wisland/app.py",
    "run.pyw",
)

# 绝对不能碰的用户数据 / 构建目录 / 版本库
PROTECTED_PATHS = (
    "config.json",
    "data",
    "models",
    ".venv",
    "packaging",
    "dist",
    "build",
    "scratch",
    ".git",
)

_VERSION_RE = re.compile(r"^[vV]?\s*(\d+(?:\.\d+)*)")
_VERSION_LINE_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")
_INIT_VERSION_RE = re.compile(r"""__version__\s*=\s*['"]([^'"]+)['"]""")
_NETWORK_ERROR = "网络连接失败，请检查网络或代理设置。"
_VERSION_ERROR = "无法读取新版本号，请检查网络或代理。"

# 同一时间只允许一个更新流程；界面重建后旧后台任务不会和新任务互相覆盖文件。
_UPDATE_LOCK = threading.Lock()


class UpdateError(Exception):
    """更新失败（可以直接展示给用户的中文说明）。

    可选属性 ``backup`` 指向本次更新已经创建的备份目录，方便用户在极端情况下手动恢复。
    """

    def __init__(self, message: str, backup: str | None = None):
        super().__init__(message)
        self.backup = backup


class UpdateCancelled(Exception):
    """用户主动取消更新。"""


class _NetworkProblem(Exception):
    """内部标记：直连网络失败，可以尝试系统代理重试。"""


# ==================================================================== 版本
def _clean_version(text: str) -> str | None:
    """提取形如 1.6.0 / v1.7.0 / 1.6.0-beta 的版本号；失败返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    m = _VERSION_RE.match(s)
    return m.group(1) if m else None


def version_tuple(text: str) -> tuple[int, ...]:
    """把版本字符串转成可比较的元组。

    支持 ``v1.6.0`` / ``1.6`` / ``1.6.0-beta``；这里刻意不补零，
    因此 ``1.6 < 1.6.0``（与项目里约定的版本比较结果一致）。
    """
    s = str(text or "").strip()
    m = _VERSION_RE.match(s)
    if not m:
        raise UpdateError(f"无法识别的版本号：{s[:40] or '（空）'}")
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError as exc:      # 理论上不会发生，保留兜底
        raise UpdateError(f"无法识别的版本号：{s[:40]}") from exc


def is_newer(a: str, b: str) -> bool:
    """a 是否比 b 新。"""
    return version_tuple(a) > version_tuple(b)


def _version_from_root(root: Path) -> str | None:
    """从仓库根目录读取版本号：优先 VERSION，其次 wisland/__init__.py。"""
    root = Path(root)
    version_file = root / "VERSION"
    if version_file.is_file():
        try:
            first = version_file.read_text(encoding="utf-8-sig").splitlines()[0].strip()
        except (OSError, IndexError):
            first = ""
        m = _VERSION_LINE_RE.match(first)
        if m:
            return m.group(1)
    init_file = root / "wisland" / "__init__.py"
    if init_file.is_file():
        try:
            m = _INIT_VERSION_RE.search(init_file.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            m = None
        if m:
            return _clean_version(m.group(1))
    return None


def _app_root() -> Path:
    """源码运行时的仓库根目录（与服务文件位置对应）。"""
    return Path(__file__).resolve().parents[2]


def current_version() -> str:
    """当前运行版本：读仓库根目录 VERSION，读不到就退回包内常量。"""
    return _version_from_root(_app_root()) or _clean_version(APP_VERSION) or APP_VERSION


def can_self_update() -> bool:
    """打包后的 exe 不能替换自己，只能下载新版本手动替换。"""
    return not bool(getattr(sys, "frozen", False))


# ==================================================================== 网络
def _client(proxy: str | None = None, timeout: float = 12.0) -> httpx.Client:
    """创建更新器专用的 httpx 客户端。"""
    kwargs: dict = {
        "follow_redirects": True,
        "trust_env": True,
        "timeout": timeout,
        "headers": {"User-Agent": USER_AGENT},
    }
    if proxy:
        kwargs["proxy"] = proxy            # httpx 0.28 使用 proxy=，不是 proxies=
    return httpx.Client(**kwargs)


def _system_proxy() -> str | None:
    """复用现有工具读取 Windows 系统代理；导入放在函数里避免模块级耦合。"""
    try:
        from ..live.http_util import system_proxy

        return system_proxy(refresh=True)
    except Exception:  # noqa: BLE001 - 代理探测失败不应影响直连结果
        return None


def _get_bytes(url: str, timeout: float) -> tuple[int, bytes]:
    """GET 一个文本接口；直连网络失败时用系统代理重试一次。"""
    try:
        with _client(timeout=timeout) as client:
            resp = client.get(url)
            return resp.status_code, resp.content
    except (httpx.HTTPError, OSError):
        proxy = _system_proxy()
        if proxy:
            try:
                with _client(proxy=proxy, timeout=timeout) as client:
                    resp = client.get(url)
                    return resp.status_code, resp.content
            except (httpx.HTTPError, OSError):
                pass
        raise UpdateError(_NETWORK_ERROR) from None


def fetch_latest_version(version_url: str = VERSION_URL, init_url: str = INIT_URL,
                         timeout: float = 12.0) -> str:
    """读取线上最新版本号；仅 VERSION 返回 404 / 内容非法时才回退 __init__.py。"""
    try:
        status, body = _get_bytes(version_url, timeout)
    except UpdateError:
        # 网络本身不通时再试另一个地址只会让用户多等一倍超时时间。
        raise UpdateError(_VERSION_ERROR) from None

    if status == 200:
        text = body.decode("utf-8-sig", errors="replace")
        first = text.splitlines()[0].strip() if text.splitlines() else ""
        m = _VERSION_LINE_RE.match(first)
        if m:
            return m.group(1)
    elif status not in (404, 410):
        raise UpdateError(f"读取新版本号失败（HTTP {status}），请稍后重试。")

    # VERSION 缺失或内容不可解析：回退到 wisland/__init__.py 的 __version__
    try:
        status, body = _get_bytes(init_url, timeout)
    except UpdateError:
        raise UpdateError(_VERSION_ERROR) from None
    if status == 200:
        text = body.decode("utf-8-sig", errors="replace")
        m = _INIT_VERSION_RE.search(text)
        if m:
            version = _clean_version(m.group(1))
            if version:
                return version
    elif status not in (404, 410):
        raise UpdateError(f"读取新版本号失败（HTTP {status}），请稍后重试。")

    # 不把一堆英文异常直接丢给用户；统一提示。
    raise UpdateError(_VERSION_ERROR)


# ==================================================================== 下载
def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("更新已取消。")


def _call(callback, *args) -> None:
    """回调异常不能打断更新流程（窗口可能已经销毁）。"""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:  # noqa: BLE001
        pass


def _download_once(url: str, part: Path, cancel: threading.Event | None, progress) -> None:
    """单次流式下载到 .part；网络错误用 _NetworkProblem 标记以便代理重试。"""
    try:
        with _client(timeout=60.0) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise UpdateError(f"下载更新包失败（HTTP {resp.status_code}），请稍后重试。")
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
                done = 0
                try:
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_bytes(64 * 1024):
                            _check_cancel(cancel)
                            if not chunk:
                                continue
                            fh.write(chunk)
                            done += len(chunk)
                            _call(progress, done, total)
                        fh.flush()
                except httpx.HTTPError as exc:
                    raise _NetworkProblem(str(exc)) from exc
                if total and done < total:
                    raise _NetworkProblem("下载连接提前中断")
                if done <= 0:
                    raise UpdateError("下载到的更新包是空的，请稍后重试。")
    except UpdateError:
        raise
    except UpdateCancelled:
        raise
    except httpx.HTTPError as exc:
        raise _NetworkProblem(str(exc)) from exc


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                raise UpdateError(f"更新包校验失败（{bad} 已损坏），请重新下载。")
    except UpdateError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise UpdateError("下载的更新包不是有效的 zip 文件，请稍后重试。") from None


def download_archive(url: str, dest: Path, cancel: threading.Event | None,
                     progress=None) -> Path:
    """流式下载更新包；先写 ``dest.part``，校验通过后再原子替换到 dest。"""
    dest = Path(dest)
    part = dest.with_suffix(".part")
    _check_cancel(cancel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _unlink(part)

    used_proxy = False
    while True:
        try:
            _download_once(url, part, cancel, progress)
            _validate_zip(part)
            _check_cancel(cancel)
            os.replace(part, dest)
            return dest
        except UpdateCancelled:
            _unlink(part)
            raise
        except _NetworkProblem:
            _unlink(part)
            if not used_proxy:
                proxy = _system_proxy()
                if proxy:
                    used_proxy = True
                    continue
            raise UpdateError(f"下载更新包失败，{_NETWORK_ERROR}") from None
        except UpdateError:
            _unlink(part)
            raise
        except OSError:
            _unlink(part)
            raise UpdateError("无法写入更新包文件，请检查磁盘空间或目录权限后重试。") from None
        except Exception as exc:  # noqa: BLE001 - 不让底层异常直接冒到界面
            _unlink(part)
            print(f"[updater] 下载失败 {type(exc).__name__}: {exc}", file=sys.stderr)
            raise UpdateError("下载更新包失败，请稍后重试。") from None


# ==================================================================== 解压
def _looks_like_repo(path: Path) -> bool:
    return (path / "VERSION").is_file() or (path / "wisland").is_dir()


def extract_archive(zip_path: Path, dest: Path,
                    cancel: threading.Event | None = None) -> Path:
    """安全解压更新包，返回实际的仓库根目录（兼容有无顶层文件夹两种打包方式）。

    安全措施：拒绝绝对路径、盘符、``..``、符号链接条目；每个文件解析后的目标必须仍在 dest 内。
    """
    zip_path, dest = Path(zip_path), Path(dest)
    _check_cancel(cancel)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError("更新包无法打开，可能已经损坏，请重新下载。") from None

    with archive:
        for info in archive.infolist():
            _check_cancel(cancel)
            raw_name = info.filename
            if not raw_name or "\x00" in raw_name:
                raise UpdateError("更新包包含非法文件名，已拒绝解压。")

            name = raw_name.replace("\\", "/")
            if name.startswith("/") or name.startswith("//") or re.match(r"^[A-Za-z]:", name):
                raise UpdateError("更新包包含绝对路径，已拒绝解压。")

            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts:
                continue
            if any(p == ".." for p in parts):
                raise UpdateError("更新包包含 .. 路径，已拒绝解压以防止覆盖更新目录之外的文件。")
            if any(":" in p for p in parts):
                raise UpdateError("更新包包含非法文件名（冒号），已拒绝解压。")

            # zip 的 unix 权限位里 0o120000 表示符号链接；Windows 常见解压漏洞之一
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise UpdateError("更新包包含符号链接条目，已拒绝解压。")

            target = base.joinpath(*parts).resolve()
            if target != base and base not in target.parents:
                raise UpdateError("更新包中的文件会写到更新目录之外，已拒绝解压。")

            if info.is_dir() or raw_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as src, open(target, "wb") as out:
                    while True:
                        _check_cancel(cancel)
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
                raise UpdateError(f"解压更新包失败：{exc}") from None

    if _looks_like_repo(dest):
        return dest
    children = [p for p in dest.iterdir() if p.is_dir()]
    if len(children) == 1 and _looks_like_repo(children[0]):
        return children[0]
    # 不是标准结构也先返回 dest，由 apply_update 给出更具体的“更新包结构不完整”提示
    return dest


# ==================================================================== 应用
def _safe_version_name(version: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", version or "unknown")
    return text.strip("._-") or "unknown"


def _unique_dir(parent: Path, name: str) -> Path:
    path = parent / name
    n = 2
    while path.exists():
        path = parent / f"{name}-{n}"
        n += 1
    return path


def _emit_status(status, text: str, percent: int | None = None) -> None:
    _call(status, text, percent)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass


def _move_path(src: Path, dst: Path) -> None:
    """目录 / 文件移动：同卷优先 rename，跨卷退回 shutil.move。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise OSError(f"目标已经存在：{dst}")
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _atomic_copy_replace(src: Path, dest: Path) -> None:
    """先复制到同目录临时文件，再 os.replace；避免替换到一半留下坏文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".new",
                                    dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except Exception:
        _unlink(tmp)
        raise


def _strip_pycache(root: Path, cancel: threading.Event | None) -> None:
    """剔除更新包里的 __pycache__ / .pyc，避免带进旧字节码。"""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        _check_cancel(cancel)
        for name in list(dirnames):
            if name == "__pycache__":
                shutil.rmtree(Path(dirpath) / name, ignore_errors=True)
                dirnames.remove(name)
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                _unlink(Path(dirpath) / name)


def _protected(rel: Path) -> bool:
    first = rel.parts[0].lower() if rel.parts else ""
    if not first:
        return True
    if first == "config.json" or first in {p.lower() for p in PROTECTED_PATHS}:
        return True
    return first.startswith(".venv")       # 覆盖 .venv312 之类


def _rollback(ops: list[tuple[str, Path, Path | None]]) -> list[str]:
    """按相反顺序回滚；返回无法回滚的条目说明。"""
    errors: list[str] = []
    for kind, target, backup in reversed(ops):
        try:
            if kind == "dir":
                _remove_path(target)
                if backup is not None:
                    if not backup.exists():
                        raise OSError("备份目录不存在")
                    _move_path(backup, target)
            else:
                if backup is not None:
                    if not backup.exists():
                        raise OSError("备份文件不存在")
                    _atomic_copy_replace(backup, target)
                else:
                    _remove_path(target)
        except Exception:  # noqa: BLE001
            errors.append(target.name)
    return errors


def _prune_backups(updates_dir: Path, keep: int = 2,
                   protect: Path | None = None) -> None:
    """只保留最近几次备份，并始终保护本次刚创建的备份。

    备份目录名形如 ``backup-YYYYMMDD-HHMMSS-v1.6.0``，按名字倒序就是时间倒序；
    不依赖 mtime（复制 / 解压工具可能把目录 mtime 改旧）。
    """
    try:
        backups = sorted(
            (p for p in updates_dir.iterdir()
             if p.is_dir() and p.name.startswith("backup-")),
            key=lambda p: p.name, reverse=True)
    except OSError:
        return

    protected = protect.resolve() if protect is not None else None
    keep_set: list[Path] = []
    if protected is not None:
        for path in backups:
            try:
                if path.resolve() == protected:
                    keep_set.append(path)
                    break
            except OSError:
                continue
    for path in backups:
        if len(keep_set) >= max(1, keep):
            break
        if path not in keep_set:
            keep_set.append(path)

    for old in backups:
        if old not in keep_set:
            shutil.rmtree(old, ignore_errors=True)


def _error_text(exc: Exception) -> str:
    if isinstance(exc, UpdateError):
        return str(exc)
    if isinstance(exc, OSError):
        detail = exc.strerror or str(exc) or "未知错误"
        return f"文件操作失败：{detail}"
    # 类型名只进日志，不能出现在用户界面。
    print(f"[updater] 意外错误 {type(exc).__name__}: {exc}", file=sys.stderr)
    return "更新过程中出现意外错误，请稍后重试。"


def apply_update(root: Path, stage: Path, expected_version: str | None = None,
                 cancel: threading.Event | None = None, status=None) -> dict:
    """把已解压的 stage 应用到 root，失败自动回滚。

    返回 ``{"version", "old_version", "backup", "changed"}``。
    """
    root, stage = Path(root).resolve(), Path(stage).resolve()
    _check_cancel(cancel)
    _emit_status(status, "正在校验更新包…", 82)

    if not root.is_dir():
        raise UpdateError("程序目录不存在，无法更新。")
    if not stage.is_dir():
        raise UpdateError("更新包还没有解压完成。")

    missing = [name for name in REQUIRED_STAGE_FILES if not (stage / name).is_file()]
    if missing:
        raise UpdateError("更新包结构不完整（缺少 " + "、".join(missing[:3]) + "），已取消更新。")

    stage_version = _version_from_root(stage)
    if not stage_version:
        raise UpdateError("更新包没有有效的版本号（VERSION 或 wisland/__init__.py）。")
    stage_tuple = version_tuple(stage_version)

    # 允许压缩包不带 VERSION（旧版本仓库常见）；用 __init__.py 里的版本补一份，
    # 这样根目录 VERSION 也会跟随更新，重启后 current_version() 不会还是旧版本。
    stage_version_file = stage / "VERSION"
    if not stage_version_file.is_file():
        try:
            stage_version_file.write_text(stage_version + "\n", encoding="utf-8")
        except OSError as exc:
            raise UpdateError(f"无法写入更新包版本文件：{exc.strerror or exc}") from None

    if expected_version:
        expected_tuple = version_tuple(expected_version)
        if stage_tuple < expected_tuple:
            raise UpdateError(f"更新包版本 {stage_version} 低于线上最新版本 "
                              f"{expected_version}，请重新下载。")

    old_version = _version_from_root(root) or current_version()

    # 更新包里的字节码一律不要（目录整体替换后旧代码也不会残留）
    _check_cancel(cancel)
    _strip_pycache(stage / "wisland", cancel)

    targets: list[tuple[Path, Path, bool]] = []
    for name in UPDATE_DIRS:
        src = stage / name
        if src.is_dir():
            targets.append((root / name, src, True))
    for name in UPDATE_FILES:
        src = stage / name
        if src.is_file():
            targets.append((root / name, src, False))
    if not targets:
        raise UpdateError("更新包里没有可更新的程序文件。")

    updates_dir = root / "data" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = _unique_dir(updates_dir,
                             f"backup-{stamp}-v{_safe_version_name(old_version)}")
    backup_created = False
    undo: list[tuple[str, Path, Path | None]] = []
    changed: list[str] = []

    try:
        for index, (target, src, is_dir) in enumerate(targets):
            _check_cancel(cancel)
            rel = target.relative_to(root)
            if _protected(rel):
                raise UpdateError(f"更新包试图修改受保护路径 {rel.as_posix()}，已拒绝。")
            percent = 84 + round(11 * index / max(1, len(targets)))
            _emit_status(status, f"正在替换程序文件…（{index + 1}/{len(targets)}）", percent)

            backup = backup_dir / rel
            if is_dir:
                had_old = target.is_dir() and not target.is_symlink()
                if had_old:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    _move_path(target, backup)
                    backup_created = True
                elif target.exists():
                    raise UpdateError(f"程序目录 {rel.as_posix()} 不是普通目录，已取消更新。")
                try:
                    _move_path(src, target)
                except Exception:
                    if had_old and backup.exists() and not target.exists():
                        _move_path(backup, target)
                    raise
                undo.append(("dir", target, backup if had_old else None))
            else:
                had_old = target.is_file()
                if target.exists() and not had_old:
                    raise UpdateError(f"程序文件 {rel.as_posix()} 不是普通文件，已取消更新。")
                if had_old:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                    backup_created = True
                _atomic_copy_replace(src, target)
                undo.append(("file", target, backup if had_old else None))
            changed.append(rel.as_posix())

        _emit_status(status, "正在清理旧备份…", 96)
        _prune_backups(updates_dir, keep=2, protect=backup_dir)
    except UpdateCancelled:
        errors = _rollback(undo)
        if errors:
            raise UpdateError("更新已取消，但回滚失败：" + "；".join(errors) + "，请从备份目录手动恢复。",
                              backup=str(backup_dir) if backup_created else None) from None
        msg = "更新已取消，已回滚到原版本。" if undo else "更新已取消，未修改任何文件。"
        raise UpdateCancelled(msg) from None
    except Exception as exc:  # noqa: BLE001
        errors = _rollback(undo)
        message = _error_text(exc)
        if errors:
            message += "；回滚失败：" + "；".join(errors) + "，请从备份目录手动恢复。"
        elif undo:
            message += "（已回滚到原版本）"
        else:
            message += "（未修改任何文件）"
        raise UpdateError(message,
                          backup=str(backup_dir) if backup_created else None) from None

    _emit_status(status, "更新完成", 100)
    return {
        "version": stage_version,
        "old_version": old_version,
        "backup": str(backup_dir) if backup_created else "",
        "changed": changed,
    }


def _run_update_locked(root: Path, cancel: threading.Event | None, progress,
                       status, version_url: str, archive_url: str) -> dict:
    root = Path(root).resolve()
    _check_cancel(cancel)

    _emit_status(status, "正在检查最新版本…", 2)
    remote_version = fetch_latest_version(version_url=version_url)
    _check_cancel(cancel)

    old_version = _version_from_root(root) or current_version()
    if version_tuple(remote_version) < version_tuple(old_version):
        raise UpdateError(f"新版本 {remote_version} 低于当前版本 {old_version}，已取消更新。")

    updates_dir = root / "data" / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    work = _unique_dir(updates_dir,
                       f"work-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    try:
        work.mkdir(parents=True)
    except OSError as exc:
        raise UpdateError(f"无法创建更新临时目录：{exc.strerror or exc}") from None

    try:
        zip_path = work / "source.zip"
        _emit_status(status, "正在下载更新包…", 5)
        download_archive(archive_url, zip_path, cancel, progress)
        _check_cancel(cancel)

        _emit_status(status, "正在校验并解压更新包…", 80)
        stage = extract_archive(zip_path, work / "stage", cancel)
        _check_cancel(cancel)

        result = apply_update(root, stage, expected_version=remote_version,
                              cancel=cancel, status=status)
        _emit_status(status, "更新完成", 100)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_update(root: Path, cancel: threading.Event | None = None, progress=None,
               status=None, version_url: str = VERSION_URL,
               archive_url: str = ARCHIVE_URL) -> dict:
    """完整更新流程：检查版本 → 下载 → 解压 → 应用；可在后台线程直接调用。"""
    if not can_self_update():
        raise UpdateError("打包版不能自动替换正在运行的程序，请打开开源仓库下载新版本。")
    if not _UPDATE_LOCK.acquire(blocking=False):
        raise UpdateError("已有更新正在进行，请稍后再试。")
    try:
        return _run_update_locked(root, cancel, progress, status, version_url, archive_url)
    finally:
        _UPDATE_LOCK.release()


# ==================================================================== 重启
_RESTART_HELPER = r'''# -*- coding: utf-8 -*-
"""wisland 更新重启助手：等待旧进程退出，再启动新版本，最后删除自己。"""
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__ROOT__)
PID = __PID__
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.GetExitCodeProcess.argtypes = (
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
kernel32.GetExitCodeProcess.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
kernel32.CloseHandle.restype = ctypes.c_int


def alive(pid):
    """Windows 上不能用 os.kill(pid, 0) 探测存活：它实际会 TerminateProcess。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def launch():
    flags = CREATE_NO_WINDOW | DETACHED_PROCESS
    vbs = ROOT / "wisland.vbs"
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    if vbs.is_file() and wscript.is_file():
        cmd = [str(wscript), str(vbs)]
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if pyw.is_file():
            exe = pyw
        cmd = [str(exe), str(ROOT / "run.pyw")]
    subprocess.Popen(cmd, cwd=str(ROOT), creationflags=flags,
                     close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


try:
    deadline = time.monotonic() + 60.0
    while alive(PID) and time.monotonic() < deadline:
        time.sleep(0.4)
    # 60 秒后旧进程仍在（可能卡在退出流程）：仍然尝试启动，wisland 的 QLockFile 会防止双开。
    launch()
finally:
    try:
        os.remove(__file__)
    except OSError:
        pass
'''


def restart_after_update(root: Path) -> bool:
    """启动一个独立的重启助手，等当前进程退出后再拉起 wisland。

    返回 True 表示助手已经成功启动（调用方可以随后退出 Qt）。
    """
    if not can_self_update():
        return False
    root = Path(root).resolve()
    if not (root / "run.pyw").is_file() and not (root / "wisland.vbs").is_file():
        return False

    source = _RESTART_HELPER.replace("__ROOT__", repr(str(root))).replace(
        "__PID__", str(os.getpid()))
    fd, helper_name = tempfile.mkstemp(prefix="wisland-restart-", suffix=".py",
                                       dir=tempfile.gettempdir())
    helper = Path(helper_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        flags = 0x08000000 | 0x00000008       # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen(
            [sys.executable, str(helper)],
            cwd=str(root),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        _unlink(helper)
        return False
