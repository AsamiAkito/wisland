# -*- coding: utf-8 -*-
"""材料类型、人工标签与 AI 标签的展示规则。"""

import json

KINDS = {
    "text": "文字 / 代码",
    "url": "网页链接",
    "image": "图片 / 截图",
    "document": "文档",
    "audio": "音频",
    "video": "视频",
}


def labels(row: dict) -> dict:
    result = json.loads(row["result"]) if row["result"] else {}
    mine = json.loads(row.get("user_tags", "[]"))
    ai = list(dict.fromkeys(t.strip() for t in result.get("tags", []) if isinstance(t, str) and t.strip()))
    location = row.get("note_path", "")
    folder = location.rsplit("/", 1)[0] if "/" in location else location
    return {
        "kind": KINDS.get(row["kind"], "其他"),
        "mine": mine,
        "ai": ai,
        "folder": folder or "本机材料箱 · 尚未写入 Obsidian",
    }


def tag_options(rows: list[dict]) -> list[tuple[str, str]]:
    options = {}
    for row in rows:
        meta = labels(row)
        for tag in meta["mine"] + meta["ai"]:
            options.setdefault(tag.casefold(), tag)
    return sorted(options.items(), key=lambda item: item[0])
