# -*- coding: utf-8 -*-
"""PyInstaller 打包版自更新：应用内下载 CNB Release 压缩包，再由同一个 exe 的 helper 模式替换文件。

整体流程（只在打包版 / ``sys.frozen`` 下启用）：

1. 界面仍用 :func:`wisland.services.updater.fetch_latest_version` 检查线上版本；
2. :func:`prepare_update` 依次探测该版本的 Release 附件，流式下载到
   ``data/updates/packaged-<ver>``，安全解压并核对包内版本与目标版本一致；
3. :func:`spawn_helper` 把任务写进 ``data/updates/packaged-job.json``，用**当前 exe**
   的 ``--apply-update`` 模式启动助手，随后界面退出；
4. 助手 :func:`run_helper` 等待旧进程退出，把 ``wisland.exe`` / ``_internal`` 改名备份到
   ``data/updates``，移入新文件并拉起新 exe；任何一步失败都尽量回滚并重新拉起旧版本；
5. 下次启动时界面读取 ``data/updates/packaged-result.json`` 展示“已更新 / 更新失败”，
   再由 :func:`cleanup_stale` 清理非当前版本的备份与过期 stage。

约束：
- 本模块不导入 PySide6，也不读写 config.json，方便在后台线程与测试里直接调用；
- helper 运行在旧 exe 里，rename 之后不能再依赖旧 ``_internal`` 的懒加载，因此只做文件操作、
  写日志和启动进程；
- ``config.json`` / ``data`` / ``models`` 永远不替换；``data/updates`` 只放更新临时文件与备份；
- CNB Releases 的 API 匿名请求会 401，所以只用公开的 download 直链，不调用 API。

Windows 上正在运行的 exe、以及包含已加载 DLL 的目录可以改名（但不能删除 / 覆盖），
helper 正是利用这一点：先改名旧文件，再移入新文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .. import __version__ as APP_VERSION
from .updater import UpdateCancelled, UpdateError, version_tuple

# ==================================================================== 常量
REPO_URL = "https://cnb.cool/shiroki.live/wisland-zus"
# 发版约定：tag 为 V{ver}，附件名见下面两个模板（onefile 兼容旧名 wisland.zip）
RELEASE_URL_TEMPLATE = REPO_URL + "/-/releases/download/V{version}/{name}"
ONEDIR_NAME = "wisland-{version}-win64.zip"
ONEFILE_NAME = "wisland-{version}-onefile-win64.zip"
ONEFILE_LEGACY_NAME = "wisland.zip"

USER_AGENT = f"wisland/{APP_VERSION} packaged-updater"

# data/updates 下的固定文件名；stage 目录形如 packaged-1.6.1
UPDATES_REL = Path("data") / "updates"
STAGE_PREFIX = "packaged-"
JOB_NAME = "packaged-job.json"
RESULT_NAME = "packaged-result.json"
LOG_NAME = "helper.log"

# 允许 best-effort 更新的说明文件（失败只记日志，绝不触发回滚）
DOC_FILES = ("README.md", "LICENSE", "config.example.json", "运行说明.md")

# 备份后缀：<name>.old-<version>-<YYYYMMDD-HHMMSS>[-序号]
BACKUP_RE = re.compile(r"\.old-(\d+(?:\.\d+)*)-(\d{8}-\d{6})(?:-\d+)?$")
MODES = ("onedir", "onefile")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

HELPER_TIMEOUT = 90.0            # 等待主进程退出的最长时间（秒）
STALE_STAGE_SECONDS = 3600.0     # 当前版本的成功 stage 至少保留 1 小时，避免和助手抢文件
_NETWORK_ERROR = "网络连接失败，请检查网络或代理设置。"
_VERSION_RE = re.compile(r"^[vV]?\s*(\d+(?:\.\d+)*)")


class _NetworkProblem(Exception):
    """内部标记：连接中断 / 超时，可以换直连重试或系统代理重试。"""


# ==================================================================== 路径与形态
def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打包版里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """打包版 = exe 所在目录；源码运行 = 仓库根目录（源码模式不会走本模块的更新流程）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def install_mode() -> str:
    """返回 ``onedir`` / ``onefile``；依据 ``_internal`` 是否存在判断。"""
    if not is_frozen():
        # 源码模式没有 _internal；这里给调用方一个稳定值，真正的更新流程不会在源码模式触发。
        return "onedir"
    return "onedir" if (app_root() / "_internal").is_dir() else "onefile"


def updates_dir(root: Path | None = None) -> Path:
    """``<root>/data/updates``。"""
    return Path(root or app_root()) / UPDATES_REL


def stage_dir(root: Path, version: str) -> Path:
    """本次下载 / 解压使用的工作目录：``<root>/data/updates/packaged-<ver>``。"""
    return updates_dir(root) / f"{STAGE_PREFIX}{_safe_version(version)}"


def job_path(root: Path | None = None) -> Path:
    """helper 任务文件。"""
    return updates_dir(root) / JOB_NAME


def result_path(root: Path | None = None) -> Path:
    """helper 写回的更新结果。"""
    return updates_dir(root) / RESULT_NAME


def _safe_version(version: str) -> str:
    """把版本号变成可以安全放进文件名 / 目录名的字符串。"""
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(version or "unknown"))
    return text.strip("._-") or "unknown"


def _fmt_size(size: int) -> str:
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ==================================================================== 版本
def _clean_version(text: str) -> str | None:
    """提取 ``1.6.1`` / ``v1.6.1`` 形式的版本号；无法识别返回 None。"""
    m = _VERSION_RE.match(str(text or "").strip())
    return m.group(1) if m else None


def _require_version(text: str) -> str:
    version = _clean_version(text)
    if not version:
        raise UpdateError(f"无法识别的版本号：{str(text or '').strip()[:40] or '（空）'}")
    return version


def _canonical_version(text: str) -> tuple[int, ...]:
    """比较用版本元组：去掉末尾的 0，允许 exe 的 ``1.6.1.0`` 与 VERSION 的 ``1.6.1`` 相等。"""
    parts = version_tuple(text)
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def _version_equal(a: str, b: str) -> bool:
    return _canonical_version(a) == _canonical_version(b)


def _version_from_dir(root: Path) -> str | None:
    """从解压目录读版本号：优先根目录 VERSION，其次 ``_internal/VERSION``。"""
    root = Path(root)
    for rel in ("VERSION", Path("_internal") / "VERSION"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        version = _clean_version(lines[0])
        if version:
            return version
    return None


def _exe_file_version(path: Path) -> str | None:
    """用 Windows 版本资源读 exe 的 FileVersion（失败返回 None，不抛异常）。

    先按 ``\\VarFileInfo\\Translation`` 的实际语言 / 代码页查 ``FileVersion``，
    再退回本项目固定的 ``080404B0`` 表，最后用 VS_FIXEDFILEINFO 的四个数字兜底。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, AttributeError):
        return None

    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR,
                                                    ctypes.POINTER(wintypes.DWORD))
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                                wintypes.DWORD, ctypes.c_void_p)
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(wintypes.UINT))
        version.VerQueryValueW.restype = wintypes.BOOL

        size = int(version.GetFileVersionInfoSizeW(str(path), None))
        if size <= 0:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None

        def query(sub_block: str) -> str | None:
            pointer = ctypes.c_void_p()
            length = wintypes.UINT()
            if not version.VerQueryValueW(buffer, sub_block,
                                          ctypes.byref(pointer), ctypes.byref(length)):
                return None
            if not pointer.value:
                return None
            return str(ctypes.wstring_at(pointer.value)).strip() or None

        # 先按翻译表（语言 + 代码页）找字符串版本号
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if (version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation",
                                   ctypes.byref(pointer), ctypes.byref(length))
                and pointer.value and length.value >= 4):
            words = ctypes.cast(pointer, ctypes.POINTER(wintypes.WORD))
            table = f"\\StringFileInfo\\{int(words[0]):04x}{int(words[1]):04x}\\FileVersion"
            text = query(table)
            if text:
                return text

        # 本项目 version_info.txt 固定使用简体中文 / 1200
        text = query("\\StringFileInfo\\080404b0\\FileVersion")
        if text:
            return text

        # 没有字符串版本时退回 VS_FIXEDFILEINFO
        class _FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
            ]

        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            if not pointer.value:
                return None
            info = ctypes.cast(pointer, ctypes.POINTER(_FixedFileInfo)).contents
            ms, ls = int(info.dwFileVersionMS), int(info.dwFileVersionLS)
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    except Exception:  # noqa: BLE001 - 读版本失败不应影响调用方，最终会给出中文提示
        return None


def _installed_version(root: Path) -> str:
    """当前安装版本：VERSION → exe 的 FileVersion → 编译进 exe 的常量。"""
    version = _version_from_dir(root)
    if version:
        return version
    version = _clean_version(_exe_file_version(Path(root) / "wisland.exe") or "")
    if version:
        return version
    return _clean_version(APP_VERSION) or APP_VERSION


# ==================================================================== 下载源
def release_candidates(version: str, mode: str | None = None) -> list[str]:
    """按优先级返回该版本的 Release 附件直链（纯字符串，不发请求）。"""
    version = _require_version(version)
    mode = mode or install_mode()
    if mode == "onedir":
        names = [ONEDIR_NAME.format(version=version)]
    elif mode == "onefile":
        # 新约定名可能还不存在，回退到旧版单文件包名
        names = [ONEFILE_NAME.format(version=version), ONEFILE_LEGACY_NAME]
    else:
        raise UpdateError(f"未知的安装形态：{mode}")
    return [RELEASE_URL_TEMPLATE.format(version=version, name=name) for name in names]


def _not_released(version: str) -> UpdateError:
    """所有候选都 404 时的统一提示；界面会原文展示。"""
    return UpdateError(
        f"v{_require_version(version)} 的打包版压缩包还没发布，请稍后重试；"
        "也可以点下方「应用开源」手动下载。")


def _client(proxy: str | None = None, timeout=15.0) -> httpx.Client:
    """创建下载器专用的 httpx 客户端（跟随 302，信任系统代理环境变量）。"""
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
    """复用现有工具读取 Windows 系统代理；失败不影响直连。"""
    try:
        from ..live.http_util import system_proxy

        return system_proxy(refresh=True)
    except Exception:  # noqa: BLE001 - 代理探测失败只应退回直连
        return None


def _probe(client: httpx.Client, url: str) -> bool:
    """只取 1 个字节判断附件是否存在，不下载整个包。"""
    with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
        status = int(response.status_code)
    if status in (200, 206):
        return True
    if status in (403, 404, 410):
        return False
    raise UpdateError(f"下载地址返回 HTTP {status}，请稍后重试。")


def release_url(version: str, mode: str | None = None, timeout: float = 15.0) -> str:
    """依次探测候选附件名，返回第一个可用的直链；全部不存在时给中文说明。"""
    candidates = release_candidates(version, mode)
    proxy: str | None = None
    tried_proxy = False
    while True:
        try:
            with _client(proxy, timeout=timeout) as client:
                for url in candidates:
                    if _probe(client, url):
                        return url
            raise _not_released(version)
        except UpdateError:
            raise
        except (httpx.HTTPError, OSError):
            if not tried_proxy:
                tried_proxy = True
                proxy = _system_proxy()
                if proxy:
                    continue
            raise UpdateError(_NETWORK_ERROR) from None


# ==================================================================== 下载
def _unlink(path: Path | str) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCancelled("更新已取消。")


def _call(callback, *args) -> None:
    """回调异常不能打断更新流程（设置窗口可能已经被销毁）。"""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:  # noqa: BLE001
        pass


def _debug(text: str) -> None:
    """写调试信息到 stderr；windowed 打包版 / helper 里 stderr 可能是 None。"""
    try:
        if sys.stderr is not None:
            print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _download_once(url: str, part: Path, cancel: threading.Event | None,
                   progress, proxy: str | None = None) -> None:
    """单次流式下载到 .part；连接中断用 _NetworkProblem 标记以便重试。"""
    _check_cancel(cancel)
    try:
        with _client(proxy, timeout=httpx.Timeout(30.0, read=60.0)) as client:
            with client.stream("GET", url) as response:
                status = int(response.status_code)
                if status != 200:
                    if status in (403, 404, 410):
                        raise UpdateError(
                            f"下载更新包失败（HTTP {status}），该版本可能已被删除，请稍后重试。")
                    raise UpdateError(f"下载更新包失败（HTTP {status}），请稍后重试。")
                try:
                    total = int(response.headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0

                done = 0
                try:
                    handle = open(part, "wb")
                except OSError as exc:
                    raise UpdateError(
                        f"无法写入更新包文件：{exc.strerror or exc}") from None
                with handle:
                    try:
                        for chunk in response.iter_bytes(64 * 1024):
                            _check_cancel(cancel)
                            if not chunk:
                                continue
                            handle.write(chunk)
                            done += len(chunk)
                            _call(progress, done, total)
                    except httpx.HTTPError as exc:
                        raise _NetworkProblem(str(exc)) from None
                    except OSError as exc:      # 磁盘写满 / 目录只读
                        raise UpdateError(
                            f"写入更新包失败：{exc.strerror or exc}") from None
                if total and done < total:
                    raise _NetworkProblem("下载连接提前中断")
                if done <= 0:
                    raise UpdateError("下载到的更新包是空的，请稍后重试。")
    except (UpdateError, UpdateCancelled, _NetworkProblem):
        raise
    except httpx.HTTPError as exc:
        raise _NetworkProblem(str(exc)) from None


def _validate_zip(path: Path) -> None:
    """zip 完整性校验：损坏的包不能进入解压流程。"""
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise UpdateError(f"更新包校验失败（{bad} 已损坏），请重新下载。")
    except UpdateError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise UpdateError("下载的更新包不是有效的 zip 文件，请稍后重试。") from None


def download(url: str, dest: Path, cancel: threading.Event | None = None,
             progress=None) -> Path:
    """流式下载到 ``dest.part``，校验通过后原子替换成 dest。

    网络失败会先用直连重试一次，再尝试系统代理；取消 / 校验失败立即清理临时文件。
    """
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    _check_cancel(cancel)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(
            f"无法创建更新目录 {dest.parent}：{exc.strerror or exc}，"
            "请检查磁盘空间或目录权限后重试。") from None
    _unlink(part)

    proxy: str | None = None
    attempt = 0
    while attempt < 3:
        if attempt == 2:                    # 直连重试一次后，再试系统代理
            proxy = _system_proxy()
            if not proxy:
                break
        current_proxy = proxy if attempt == 2 else None
        attempt += 1
        try:
            _download_once(url, part, cancel, progress, current_proxy)
            _validate_zip(part)
            _check_cancel(cancel)
            try:
                size = part.stat().st_size
                if size <= 0:
                    raise UpdateError("下载到的更新包是空的，请稍后重试。")
                os.replace(part, dest)
            except OSError as exc:
                raise UpdateError(
                    f"无法保存更新包：{exc.strerror or exc}，请检查磁盘空间。") from None
            return dest
        except UpdateCancelled:
            _unlink(part)
            raise
        except UpdateError:
            _unlink(part)
            raise
        except _NetworkProblem:
            _unlink(part)
            continue
        except OSError:
            _unlink(part)
            raise UpdateError(
                "无法写入更新包文件，请检查磁盘空间或目录权限后重试。") from None
        except Exception as exc:  # noqa: BLE001 - 任何意外都要清掉大文件再报中文错误
            _unlink(part)
            _debug(f"[packaged_update] 下载失败 {type(exc).__name__}: {exc}")
            raise UpdateError("下载更新包失败，请稍后重试。") from None

    raise UpdateError(f"下载更新包失败，{_NETWORK_ERROR}")


# ==================================================================== 解压与校验
def extract_archive(zip_path: Path, dest: Path,
                    cancel: threading.Event | None = None) -> None:
    """安全解压：拒绝绝对路径、盘符、``..``、冒号、符号链接与目录穿越。"""
    zip_path, dest = Path(zip_path), Path(dest)
    _check_cancel(cancel)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(
            f"无法创建解压目录 {dest}：{exc.strerror or exc}，请检查磁盘空间。") from None
    base = dest.resolve()

    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError("更新包无法打开，可能已经损坏，请重新下载。") from None

    with archive:
        try:
            for info in archive.infolist():
                _check_cancel(cancel)
                raw_name = info.filename
                if not raw_name or "\x00" in raw_name:
                    raise UpdateError("更新包包含非法文件名，已拒绝解压。")

                name = raw_name.replace("\\", "/")
                if name.startswith("/") or name.startswith("//") or re.match(r"^[A-Za-z]:", name):
                    raise UpdateError("更新包包含绝对路径，已拒绝解压。")

                parts = [part for part in name.split("/") if part not in ("", ".")]
                if not parts:
                    continue
                if any(part == ".." for part in parts):
                    raise UpdateError("更新包包含 .. 路径，已拒绝解压以防止覆盖更新目录之外的文件。")
                if any(":" in part for part in parts):
                    raise UpdateError("更新包包含非法文件名（冒号），已拒绝解压。")

                # zip 的 unix 权限位里 0o120000 表示符号链接；Windows 常见解压漏洞之一
                mode = (info.external_attr >> 16) & 0xFFFF
                if (mode & 0xF000) == 0xA000:
                    raise UpdateError("更新包包含符号链接条目，已拒绝解压。")

                target = base.joinpath(*parts).resolve()
                if target != base and base not in target.parents:
                    raise UpdateError("更新包中的文件会写到更新目录之外，已拒绝解压。")

                if info.is_dir() or raw_name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with archive.open(info) as source, open(target, "wb") as output:
                        while True:
                            _check_cancel(cancel)
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
                    raise UpdateError(f"解压更新包失败：{exc}") from None
        except UpdateError:
            raise
        except OSError as exc:
            raise UpdateError(f"解压更新包失败：{exc.strerror or exc}") from None


def _find_layout(dest: Path, mode: str) -> Path:
    """在解压目录里找到真正含 ``wisland.exe`` 的那一层（兼容有无顶层文件夹）。"""
    def matches(path: Path) -> bool:
        if not (path / "wisland.exe").is_file():
            return False
        return (path / "_internal").is_dir() if mode == "onedir" else True

    candidates = [dest]
    try:
        candidates += [path for path in sorted(dest.iterdir()) if path.is_dir()]
    except OSError as exc:
        raise UpdateError(f"无法读取更新包目录：{exc.strerror or exc}") from None

    for path in candidates:
        if not matches(path):
            continue
        if mode == "onefile" and (path / "_internal").is_dir():
            raise UpdateError(
                "更新包是文件夹版（onedir），与当前单文件版（onefile）不匹配，已取消更新。")
        return path

    if mode == "onedir":
        raise UpdateError(
            "更新包结构不正确：没有找到 wisland.exe 和 _internal 目录，"
            "请确认下载的是文件夹版压缩包。")
    raise UpdateError("更新包结构不正确：没有找到 wisland.exe，请确认下载的是单文件版压缩包。")


def _ensure_free_space(path: Path, zip_size: int) -> None:
    """解压前估算磁盘空间（按 zip 的 3.5 倍 + 32 MB 开销）。"""
    needed = int(max(0, zip_size) * 3.5) + 32 * 1024 * 1024
    # 目标目录可能还没创建，向上找到最近的已存在目录再取磁盘剩余空间
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = int(shutil.disk_usage(str(probe)).free)
    except OSError:
        return
    if free < needed:
        raise UpdateError(
            f"磁盘空间不足：解压更新包大约需要 {_fmt_size(needed)}，"
            f"当前可用 {_fmt_size(free)}，请清理磁盘后重试。")


def package_version(stage: Path) -> str:
    """读取更新包版本：stage 根 / ``_internal`` 的 VERSION，否则读 exe 资源。"""
    version = _version_from_dir(stage)
    if version:
        return version
    version = _clean_version(_exe_file_version(Path(stage) / "wisland.exe") or "")
    if version:
        return version
    raise UpdateError(
        "无法读取更新包里 wisland.exe 的版本信息，已取消更新（请确认下载的是官方发布包）。")


def extract_package(zip_path: Path, dest: Path, mode: str, expected_version: str,
                    cancel: threading.Event | None = None) -> Path:
    """解压并校验布局与版本，返回含 ``wisland.exe`` 的目录。"""
    zip_path, dest = Path(zip_path), Path(dest)
    expected_version = _require_version(expected_version)
    _check_cancel(cancel)
    if not zip_path.is_file():
        raise UpdateError("更新包文件不存在，可能已被清理，请重新下载。")
    try:
        zip_size = zip_path.stat().st_size
    except OSError as exc:
        raise UpdateError(f"无法读取更新包：{exc.strerror or exc}") from None
    if zip_size <= 0:
        raise UpdateError("更新包是空文件，请重新下载。")

    _ensure_free_space(dest, zip_size)
    extract_archive(zip_path, dest, cancel)
    _check_cancel(cancel)

    stage = _find_layout(dest, mode)
    try:
        if (stage / "wisland.exe").stat().st_size <= 0:
            raise UpdateError("更新包里的 wisland.exe 是空文件，已取消更新。")
    except OSError as exc:
        raise UpdateError(f"无法读取更新包里的 wisland.exe：{exc.strerror or exc}") from None

    found = package_version(stage)
    try:
        same = _version_equal(found, expected_version)
    except UpdateError as exc:
        raise UpdateError(f"无法校验更新包版本：{exc}") from None
    if not same:
        raise UpdateError(
            f"更新包版本是 {found}，与目标版本 {expected_version} 不一致，已取消更新。")
    return stage


# ==================================================================== 准备更新
def prepare_update(version: str, cancel: threading.Event | None = None, progress=None,
                   status=None, root: Path | None = None,
                   mode: str | None = None) -> dict:
    """完整的前台准备工作：找附件 → 下载 → 解压 → 校验。

    返回的 dict 直接交给 :func:`spawn_helper` 即可；失败 / 取消时会把 stage 目录整个删掉，
    避免在 ``data/updates`` 里留下 100 MB 以上的残留文件。
    """
    version = _require_version(version)
    root = Path(root).resolve() if root is not None else app_root()
    mode = mode or install_mode()
    _check_cancel(cancel)

    if version_tuple(version) < version_tuple(APP_VERSION):
        raise UpdateError(
            f"目标版本 {version} 低于当前版本 {APP_VERSION}，已取消更新。")

    _call(status, f"正在查找 v{version} 的打包版压缩包…", 3)
    url = release_url(version, mode)
    _call(status, f"正在下载 v{version} 的打包版压缩包…", 5)

    stage = stage_dir(root, version)
    if stage.exists():
        try:
            shutil.rmtree(stage)
        except OSError as exc:
            raise UpdateError(
                f"无法清理旧的更新目录 {stage}：{exc.strerror or exc}，"
                "请关闭占用它的程序后重试。") from None
    try:
        stage.mkdir(parents=True)
    except OSError as exc:
        raise UpdateError(
            f"无法创建更新目录 {stage}：{exc.strerror or exc}，"
            "请检查磁盘空间或目录权限。") from None

    zip_path = stage / f"wisland-{version}-{mode}.zip"
    try:
        # 下载阶段的进度由具体字节数回调驱动，状态文案只负责阶段切换
        download(url, zip_path, cancel, progress)
        _check_cancel(cancel)
        _call(status, "正在解压并校验更新包…", 82)
        layout = extract_package(zip_path, stage / "extract", mode, version, cancel)
        _check_cancel(cancel)
        _call(status, "更新包校验通过，准备退出替换…", 98)
        return {
            "version": version,
            "mode": mode,
            "root": str(root),
            "stage": str(layout),
            "work": str(stage),
            "zip": str(zip_path),
            "url": url,
            "old_version": _installed_version(root),
        }
    except (UpdateCancelled, UpdateError):
        shutil.rmtree(stage, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - 不让底层异常直接冒到界面
        shutil.rmtree(stage, ignore_errors=True)
        _debug(f"[packaged_update] 准备更新失败 {type(exc).__name__}: {exc}")
        raise UpdateError("准备更新包时出现意外错误，请稍后重试。") from None


def discard_stage(root: Path | None, version: str) -> None:
    """删除某个版本留下的 stage 目录（失败忽略；可放在后台线程里调用）。"""
    try:
        shutil.rmtree(stage_dir(Path(root or app_root()), version), ignore_errors=True)
    except OSError:
        pass


# ==================================================================== 任务与结果
def _write_json(path: Path, data: dict) -> None:
    """先写临时文件再 replace，避免 helper 读到写了一半的 job / result。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except Exception:
        _unlink(temp)
        raise


def _write_result(root: Path, result: dict) -> None:
    try:
        _write_json(result_path(root), result)
    except OSError:
        pass


def read_result(root: Path | None = None) -> dict | None:
    """读取上次更新结果；没有 / 损坏 / 格式不对都返回 None。"""
    path = result_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") not in ("ok", "error"):
        return None
    return data


def clear_result(root: Path | None = None) -> None:
    """界面展示过结果后清掉，避免每次打开设置都重复提示。"""
    _unlink(result_path(root))


def clear_job(root: Path | None = None) -> None:
    """清理没有被 helper 读取的任务文件（例如启动助手失败时）。"""
    _unlink(job_path(root))


def spawn_helper(root: Path, job: dict) -> bool:
    """把任务写到 ``data/updates/packaged-job.json``，再用当前 exe 启动 helper。

    返回 True 表示助手进程已经成功创建；调用方可以在约 200 ms 后退出 Qt。helper 自己
    会等当前进程真正退出后才动文件，所以这里不需要再等待。
    """
    if not isinstance(job, dict):
        return False
    root = Path(root).resolve()
    data = dict(job)
    data["root"] = str(root)
    data["pid"] = os.getpid()
    data.setdefault("created", time.strftime("%Y-%m-%d %H:%M:%S"))

    mode = str(data.get("mode") or "")
    version = str(data.get("version") or "")
    if mode not in MODES or not version:
        return False

    allowed = updates_dir(root).resolve()
    try:
        stage = Path(str(data.get("stage") or "")).resolve()
    except (OSError, RuntimeError):
        return False
    if stage != allowed and allowed not in stage.parents:
        return False
    if not (stage / "wisland.exe").is_file():
        return False
    if mode == "onedir" and not (stage / "_internal").is_dir():
        return False
    data["stage"] = str(stage)

    work = str(data.get("work") or "").strip()
    if work:
        try:
            work_path = Path(work).resolve()
        except (OSError, RuntimeError):
            return False
        if work_path != allowed and allowed not in work_path.parents:
            return False
        data["work"] = str(work_path)

    data.setdefault("old_version", _installed_version(root))

    path = job_path(root)
    try:
        _write_json(path, data)
    except (OSError, TypeError, ValueError):
        return False

    flags = CREATE_NO_WINDOW | DETACHED_PROCESS
    try:
        subprocess.Popen(
            [sys.executable, "--apply-update", str(path)],
            cwd=str(root),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001 - 启动助手失败只需返回 False，界面会给出中文提示
        _unlink(path)
        return False


# ==================================================================== helper
def _append_log(root: Path | None, text: str) -> None:
    """helper 日志：``data/updates/helper.log``；写日志失败绝不能影响替换流程。"""
    if root is None:
        return
    try:
        folder = updates_dir(root)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """Windows 上判断进程是否还活着。

    不能用 ``os.kill(pid, 0)``：在 Windows 上它实际会调用 TerminateProcess。
    """
    if os.name != "nt" or pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                                ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 打不开句柄不一定代表进程已退出：权限不足时保守地当作“仍在运行”，
            # 让 helper 等满超时后放弃替换，而不是去抢正被占用的文件。
            if int(ctypes.get_last_error()) == 5:       # ERROR_ACCESS_DENIED
                return True
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def _wait_for_exit(pid: int, timeout: float = HELPER_TIMEOUT) -> bool:
    """轮询等待主进程退出；返回 True 表示已经退出。"""
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.25)
    return not _process_alive(pid)


def _launch_exe(exe: Path, root: Path) -> None:
    """与 spawn_helper 相同的方式拉起一个 exe（不做等待）。"""
    subprocess.Popen(
        [str(exe)],
        cwd=str(root),
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _load_job(job_file: Path) -> dict | None:
    try:
        data = json.loads(job_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _maybe_root(job: dict) -> Path | None:
    try:
        root = Path(str(job.get("root") or ""))
    except (TypeError, ValueError):
        return None
    return root if root.is_dir() else None


def _root_from_job_path(job_file: Path) -> Path | None:
    """从 ``<root>/data/updates/packaged-job.json`` 反推 root；形状不对返回 None。"""
    try:
        path = job_file.resolve()
    except OSError:
        return None
    parents = path.parents
    if len(parents) < 3:
        return None
    if parents[0].name.lower() != "updates" or parents[1].name.lower() != "data":
        return None
    return parents[2]


def _validate_job(job: dict) -> tuple[Path, Path, Path | None, str, str, int, str]:
    """校验 job；返回 (root, stage, work, mode, version, pid, old_version)。"""
    try:
        pid = int(job.get("pid") or 0)
    except (TypeError, ValueError):
        raise UpdateError("任务文件里的进程号无效。") from None
    if pid <= 0:
        raise UpdateError("任务文件里的进程号无效。")

    root_raw = str(job.get("root") or "").strip()
    stage_raw = str(job.get("stage") or "").strip()
    mode = str(job.get("mode") or "").strip()
    version_raw = str(job.get("version") or "").strip()
    if not root_raw or not stage_raw or not version_raw:
        raise UpdateError("任务文件缺少 root / stage / version。")
    if mode not in MODES:
        raise UpdateError(f"任务文件里的安装形态无效：{mode or '（空）'}。")
    version = _require_version(version_raw)

    root = Path(root_raw)
    if not root.is_dir():
        raise UpdateError(f"程序目录不存在：{root}")
    root = root.resolve()
    if is_frozen() and root != Path(sys.executable).resolve().parent:
        raise UpdateError(f"任务文件里的程序目录与当前 exe 不一致：{root}")

    allowed = updates_dir(root).resolve()
    try:
        stage = Path(stage_raw).resolve()
    except (OSError, RuntimeError):
        raise UpdateError(f"更新包目录无效：{stage_raw}") from None
    if stage != allowed and allowed not in stage.parents:
        raise UpdateError("更新包目录不在 data/updates 下，已拒绝执行。")

    work_raw = str(job.get("work") or "").strip()
    work: Path | None = None
    if work_raw:
        try:
            work = Path(work_raw).resolve()
        except (OSError, RuntimeError):
            raise UpdateError(f"更新工作目录无效：{work_raw}") from None
        if work != allowed and allowed not in work.parents:
            raise UpdateError("更新工作目录不在 data/updates 下，已拒绝执行。")

    old_version = _clean_version(str(job.get("old_version") or ""))
    if not old_version:
        old_version = _installed_version(root)
    return root, stage, work, mode, version, pid, old_version


def _unique_path(path: Path) -> Path:
    """备份重名时追加 ``-2``、``-3``；BACKUP_RE 容忍这个序号。"""
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise UpdateError(f"备份文件名重复过多：{path.name}")


def _move_path(source: Path, target: Path) -> None:
    """同卷优先 rename，跨卷退回 shutil.move；目标必须不存在。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise OSError(f"目标已经存在：{target}")
    try:
        os.replace(source, target)
    except OSError:
        shutil.move(str(source), str(target))


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _rollback_files(placed: list[Path],
                    backups: list[tuple[Path, Path]]) -> list[str]:
    """按相反顺序回滚：先移走新文件，再把备份改名回来。返回无法回滚的条目。"""
    errors: list[str] = []
    for path in reversed(placed):
        try:
            _remove_path(path)
        except OSError as exc:
            errors.append(f"{path.name}：{exc.strerror or exc}")
    for backup, original in reversed(backups):
        if original.exists():
            errors.append(f"{original.name} 已存在，未覆盖")
            continue
        try:
            _move_path(backup, original)
        except (OSError, UpdateError) as exc:
            errors.append(f"{original.name}：{exc}")
    return errors


def _cleanup_work(work: Path | None) -> None:
    """删除本次 stage（zip + 解压目录）；失败留给下次启动的 cleanup_stale。"""
    if work is None:
        return
    try:
        shutil.rmtree(work, ignore_errors=True)
    except OSError:
        pass


def _try_launch_old(root: Path | None) -> None:
    """尽量重新拉起旧版本；打包版只允许启动当前 exe 所在目录里的 wisland.exe。"""
    if root is None:
        return
    try:
        root = root.resolve()
        if is_frozen() and root != Path(sys.executable).resolve().parent:
            return
        exe = root / "wisland.exe"
        if not exe.is_file():
            return
        _launch_exe(exe, root)
        _append_log(root, "已尝试重新启动旧版本。")
    except Exception as exc:  # noqa: BLE001 - 重启失败只能写日志，不能让 helper 崩溃
        _append_log(root, f"重新启动旧版本失败：{exc}")


def _helper_failure(root: Path, job_file: Path, work: Path | None, mode: str, version: str,
                    old_version: str, message: str, backups: list[tuple[Path, Path]],
                    launch_old: bool) -> int:
    """写失败结果、尽量重新拉起旧 exe，并清理 stage。"""
    _append_log(root, f"更新失败：{message}")
    result = {
        "status": "error",
        "mode": mode,
        "version": version,
        "old_version": old_version,
        "message": message,
        "backup": str(updates_dir(root)),
        "backups": [str(path) for path, _original in backups],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_result(root, result)

    if launch_old:
        # 即使回滚有残留也尝试拉起：QLockFile 会阻止双开，重复启动是安全的
        _try_launch_old(root)

    _cleanup_work(work)
    _unlink(job_file)
    return 1


def _run_helper_body(job_path: str | os.PathLike) -> int:
    """``--apply-update`` 模式的实际流程：等旧进程退出，替换文件，拉起新 exe。

    返回进程退出码（0 成功 / 1 失败）。这里不初始化 Qt、不碰 QLockFile、不弹窗，
    也不在 rename 之后导入任何包内模块，避免旧 ``_internal`` 改名后懒加载失败。
    """
    job_file = Path(job_path)
    job = _load_job(job_file)
    if job is None:
        # 任务文件损坏时不知道 stage 在哪，但可以从固定路径反推 root，尽量把旧版本拉起来
        root = _root_from_job_path(job_file)
        _append_log(root, "更新任务文件损坏，已跳过本次替换。")
        _try_launch_old(root)
        _unlink(job_file)
        return 1

    try:
        root, stage, work, mode, version, pid, old_version = _validate_job(job)
    except UpdateError as exc:
        root = _maybe_root(job)
        _append_log(root, f"更新任务无效：{exc}")
        _try_launch_old(root)
        _unlink(job_file)
        return 1

    _append_log(root, f"收到更新任务：v{version}（{mode}），等待主进程 {pid} 退出…")
    try:
        exited = _wait_for_exit(pid, HELPER_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 探测失败按“仍未退出”处理，避免误替换
        _append_log(root, f"等待主进程退出时出错：{exc}")
        exited = False
    if not exited:
        message = (f"旧进程没有在 {HELPER_TIMEOUT:.0f} 秒内退出，"
                   "为避免文件被占用已取消替换；已尝试重新启动旧版本，"
                   "若没有反应请手动重启 wisland。")
        _append_log(root, message)
        _write_result(root, {
            "status": "error",
            "mode": mode,
            "version": version,
            "old_version": old_version,
            "message": message,
            "backup": str(updates_dir(root)),
            "backups": [],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        # 旧进程可能只是卡在退出流程：QLockFile 会让重复实例自行退出，
        # 但万一它其实已经退出，这次拉起就能把旧版本恢复回来。
        _try_launch_old(root)
        _unlink(job_file)
        return 1

    # 旧进程完全退出后再等一小会儿，让配置 / 日志 / 锁文件句柄释放
    time.sleep(0.3)
    _append_log(root, "主进程已退出，开始替换文件。")

    if not stage.is_dir():
        return _helper_failure(
            root, job_file, work, mode, version, old_version,
            "更新包目录不存在（可能已被清理），已取消更新。", [], True)
    if not (stage / "wisland.exe").is_file():
        return _helper_failure(
            root, job_file, work, mode, version, old_version,
            "更新包缺少 wisland.exe，已取消更新。", [], True)
    if mode == "onedir" and not (stage / "_internal").is_dir():
        return _helper_failure(
            root, job_file, work, mode, version, old_version,
            "更新包缺少 _internal 目录，已取消更新。", [], True)

    updates = updates_dir(root)
    try:
        updates.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _helper_failure(
            root, job_file, work, mode, version, old_version,
            f"无法创建备份目录：{exc.strerror or exc}", [], True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_old = _safe_version(old_version)
    placed: list[Path] = []
    backups: list[tuple[Path, Path]] = []

    def backup(source: Path) -> None:
        target = _unique_path(updates / f"{source.name}.old-{safe_old}-{stamp}")
        _move_path(source, target)
        backups.append((target, source))
        _append_log(root, f"已备份 {source.name} → {target}")

    try:
        if mode == "onedir":
            old_internal = root / "_internal"
            old_exe = root / "wisland.exe"
            new_internal = stage / "_internal"
            new_exe = stage / "wisland.exe"
            if not old_internal.is_dir():
                raise UpdateError("当前安装目录缺少 _internal 目录，无法按文件夹版更新。")
            if not old_exe.is_file():
                raise UpdateError("当前安装目录缺少 wisland.exe，无法更新。")
            backup(old_internal)
            backup(old_exe)
            _move_path(new_internal, root / "_internal")
            placed.append(root / "_internal")
            _move_path(new_exe, root / "wisland.exe")
            placed.append(root / "wisland.exe")
        else:
            old_exe = root / "wisland.exe"
            new_exe = stage / "wisland.exe"
            if not old_exe.is_file():
                raise UpdateError("当前安装目录缺少 wisland.exe，无法更新。")
            backup(old_exe)
            _move_path(new_exe, root / "wisland.exe")
            placed.append(root / "wisland.exe")
        _append_log(root, "程序文件替换完成。")
    except Exception as exc:  # noqa: BLE001 - 任何失败都要回滚并重新拉起旧版本
        errors = _rollback_files(placed, backups)
        if isinstance(exc, UpdateError):
            message = str(exc)
        else:
            message = f"文件替换失败：{getattr(exc, 'strerror', None) or exc}"
        if errors:
            message += ("；回滚未完成：" + "；".join(errors)
                        + f"；备份位置：{updates}")
            _append_log(root, "回滚未完成：" + "；".join(errors))
        else:
            message += "（已回滚到旧版本）"
        return _helper_failure(root, job_file, work, mode, version, old_version,
                               message, backups, True)

    # README / LICENSE 等说明文件属于 best-effort：失败只记日志，绝不回滚
    for name in DOC_FILES:
        source = stage / name
        if not source.is_file():
            continue
        try:
            shutil.copy2(source, root / name)
            _append_log(root, f"已更新说明文件 {name}")
        except OSError as exc:
            _append_log(root, f"说明文件 {name} 更新失败（已忽略）：{exc.strerror or exc}")

    result = {
        "status": "ok",
        "mode": mode,
        "version": version,
        "old_version": old_version,
        "backup": str(updates),
        "backups": [str(path) for path, _original in backups],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 先写结果再启动新 exe：新进程启动后立刻就能读到“已更新”提示
    _write_result(root, result)

    try:
        _launch_exe(root / "wisland.exe", root)
    except Exception as exc:  # noqa: BLE001 - 启动失败也要走回滚，不能让 helper 直接崩掉
        errors = _rollback_files(placed, backups)
        message = f"程序文件已替换，但无法启动新版本：{getattr(exc, 'strerror', None) or exc}"
        if errors:
            message += ("；回滚未完成：" + "；".join(errors)
                        + f"；备份位置：{updates}")
            _append_log(root, "启动新版本失败且回滚未完成：" + "；".join(errors))
        else:
            message += "（已回滚到旧版本）"
        _append_log(root, message)
        _write_result(root, {
            "status": "error",
            "mode": mode,
            "version": version,
            "old_version": old_version,
            "message": message,
            "backup": str(updates),
            "backups": [str(path) for path, _original in backups],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        _try_launch_old(root)
        _cleanup_work(work)
        _unlink(job_file)
        return 1

    _append_log(root, f"已启动新版本 v{version}。")
    _cleanup_work(work)
    _unlink(job_file)
    return 0


def run_helper(job_path: str | os.PathLike) -> int:
    """``--apply-update`` 模式入口：即使出现意外错误也要写日志、拉起旧版本并返回 1。"""
    try:
        return _run_helper_body(job_path)
    except Exception as exc:  # noqa: BLE001 - helper 没有控制台，错误只能进 helper.log
        root: Path | None = None
        try:
            job_file = Path(job_path)
            root = _root_from_job_path(job_file)
            if root is None:
                job = _load_job(job_file)
                root = _maybe_root(job) if job else None
            _append_log(root, f"helper 意外错误 {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        _try_launch_old(root)
        return 1


# ==================================================================== 清理
def _backup_version(name: str) -> str | None:
    """从 ``_internal.old-1.6.0-20260101-010101`` 里取出版本号。"""
    match = BACKUP_RE.search(name)
    return match.group(1) if match else None


def _backup_stamp(name: str) -> str | None:
    """从 ``_internal.old-1.6.0-20260101-010101`` 里取出时间戳（用于分组）。"""
    match = BACKUP_RE.search(name)
    return match.group(2) if match else None


def _job_protected_paths(root: Path) -> set[str]:
    """读取尚未完成的 helper 任务，返回 stage / work 的 normcase 绝对路径。

    helper 可能在等待旧进程退出；这段时间不能把它的 stage 当垃圾清掉。
    """
    job = _load_job(job_path(root))
    if not job:
        return set()
    protected: set[str] = set()
    for key in ("stage", "work"):
        raw = str(job.get(key) or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
        except (OSError, RuntimeError):
            continue
        protected.add(os.path.normcase(str(path)))
    return protected


def _is_protected_path(path: Path, protected: set[str]) -> bool:
    """判断 path 是否与受保护目录相同、在其内部或包含它。"""
    try:
        key = os.path.normcase(str(path.resolve()))
    except (OSError, RuntimeError):
        return True                 # 无法判断时保守保留
    for item in protected:
        if key == item:
            return True
        if key.startswith(item + os.sep) or item.startswith(key + os.sep):
            return True
    return False


def cleanup_stale(root: Path | None = None,
                  current_version: str | None = None) -> list[str]:
    """清理过期备份与 stage；返回删除的路径（失败全部忽略）。

    备份策略：
    - 与当前运行版本相同的备份永远保留（回滚用）；
    - 其他版本的备份按时间戳分组，**只保留最新一组**（一次更新留下的
      ``_internal`` / ``wisland.exe`` 属于同一组），更早的组删除；
    - ``packaged-result.json`` 引用中的备份本次保留，避免界面正在提示
      “旧版备份：…” 时路径已经被删。

    stage 策略：``data/updates/packaged-*`` 只有 mtime 超过
    ``STALE_STAGE_SECONDS`` 才删；``packaged-job.json`` 里的 stage / work
    目录受保护，避免 helper 等待旧进程退出时被误删。
    """
    root = Path(root).resolve() if root is not None else app_root()
    version = current_version or APP_VERSION
    try:
        current = _canonical_version(version)
    except UpdateError:
        return []

    removed: list[str] = []
    result_protected: set[str] = set()
    try:
        result = read_result(root)
        if result and result.get("status") == "ok":
            items = result.get("backups") or []
            if isinstance(items, (list, tuple)):
                for item in items:
                    result_protected.add(
                        os.path.normcase(str(Path(str(item)).resolve())))
    except (OSError, ValueError, RuntimeError):
        result_protected = set()

    def remove(path: Path) -> None:
        try:
            if os.path.normcase(str(path.resolve())) in result_protected:
                return
            _remove_path(path)
            removed.append(str(path))
        except (OSError, RuntimeError):
            pass

    updates = updates_dir(root)

    # 1) 备份：同版本永远保留；其他版本按时间戳分组，只留最新一组
    groups: dict[str, list[Path]] = {}
    for folder in (updates, root):
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            found = _backup_version(path.name)
            if not found:
                continue
            try:
                if _canonical_version(found) == current:
                    continue                # 同版本备份保留，供回滚
            except UpdateError:
                continue
            stamp = _backup_stamp(path.name)
            if stamp:
                groups.setdefault(stamp, []).append(path)

    if groups:
        latest = max(groups)               # 时间戳格式固定，可直接按字符串比较
        for stamp, paths in groups.items():
            if stamp == latest:
                continue
            for path in paths:
                remove(path)

    # 2) 下载 / 解压目录：只按 mtime 判断，helper 正在使用的目录受保护
    try:
        stages = [path for path in updates.iterdir()
                  if path.is_dir() and path.name.startswith(STAGE_PREFIX)]
    except OSError:
        stages = []
    job_protected = _job_protected_paths(root)
    now = time.time()
    for path in stages:
        if _is_protected_path(path, job_protected):
            continue
        try:
            expired = (now - path.stat().st_mtime) > STALE_STAGE_SECONDS
        except OSError:
            expired = False
        if expired:
            remove(path)

    # 3) 下载中断留下的 .part
    try:
        parts = list(updates.glob("*.part"))
    except OSError:
        parts = []
    for path in parts:
        try:
            if (now - path.stat().st_mtime) > STALE_STAGE_SECONDS:
                remove(path)
        except OSError:
            pass

    return removed
