# -*- coding: utf-8 -*-
"""一次性下载推荐词典（之后完全离线）。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .dictionary import DICT_DIR

# (说明, GitHub 仓库, 资源文件名关键字)
RECOMMENDED = [
    ("Jitendex 日中词典", "greyindex/jitendex-yomitan-zh", "full-review-dedup"),
    ("JMdict 英文简明版", "yomidevs/jmdict-yomitan", "JMdict_english_legacy.zip"),
]


def _asset_url(client, repo: str, key: str) -> tuple[str, str, int]:
    r = client.get(f"https://api.github.com/repos/{repo}/releases/latest",
                   headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    for a in r.json().get("assets", []):
        name = a.get("name", "")
        if key in name and name.endswith(".zip"):
            return a["browser_download_url"], name, int(a.get("size") or 0)
    raise RuntimeError(f"{repo} 的最新发布里没有找到 {key}")


def download_recommended(progress: Callable[[str], None] | None = None) -> list[Path]:
    import httpx

    DICT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0)) as client:
        for label, repo, key in RECOMMENDED:
            url, name, size = _asset_url(client, repo, key)
            dest = DICT_DIR / name
            if dest.exists() and size and dest.stat().st_size == size:
                out.append(dest)
                continue
            tmp = dest.with_suffix(".part")
            got = 0
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or size or 0)
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        if progress and total:
                            progress(f"下载 {label}：{got / 1e6:.1f} / {total / 1e6:.1f} MB")
            tmp.replace(dest)
            out.append(dest)
    return out
