# -*- coding: utf-8 -*-
"""把单词本导出到 Anki / AnkiDroid。

两条路线（都不需要互联网）：
1. 导出 .apkg（genanki）：把文件传到手机，用 AnkiDroid 打开即可导入。
   每张卡用固定 GUID，重复导出不会产生重复卡片。
2. 局域网直推：手机装 AnkiConnect Android，PC 通过 Wi-Fi 调用它的
   AnkiConnect 兼容接口（addNote / storeMediaFile），卡片直接进 AnkiDroid。
"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import httpx

from ..live.http_util import make_client

from .wordbook import MEDIA_DIR, Card

MODEL_ID = 1730000000417          # 固定值：同一个笔记类型，重复导入不会新建类型
MODEL_NAME = "译声·日语挖词"
FIELDS = ["Word", "Reading", "Meaning", "Sentence", "SentenceMeaning",
          "SentenceAudio", "Picture", "Source", "Notes"]

CSS = """
.card { font-family: "Hiragino Sans","Noto Sans CJK JP","Yu Gothic","Microsoft YaHei",sans-serif;
        font-size: 20px; text-align: center; color: #1f2430; background: #fbfbfd; }
.word { font-size: 46px; margin-top: 18px; }
.reading { font-size: 24px; color: #d95482; margin: 6px 0 12px; }
.sentence { font-size: 24px; margin: 14px 8px; line-height: 1.6; }
.sentence b { color: #d95482; }
.meaning { text-align: left; display: inline-block; max-width: 680px; font-size: 18px; line-height: 1.6; }
.trans { color: #667085; font-size: 17px; margin: 6px 8px 14px; }
.src { color: #9aa3b5; font-size: 13px; margin-top: 14px; }
img { max-width: 92%; max-height: 300px; border-radius: 8px; margin-top: 10px; }
.night_mode .card { color: #e9ecf4; background: #1d2230; }
"""

FRONT = """<div class="word">{{Word}}</div>
<div class="sentence">{{Sentence}}</div>
{{SentenceAudio}}"""

BACK = """{{FrontSide}}
<hr id=answer>
<div class="reading">{{Reading}}</div>
<div class="meaning">{{Meaning}}</div>
{{#SentenceMeaning}}<div class="trans">{{SentenceMeaning}}</div>{{/SentenceMeaning}}
{{#Picture}}<div>{{Picture}}</div>{{/Picture}}
{{#Notes}}<div class="trans">{{Notes}}</div>{{/Notes}}
<div class="src">{{Source}}</div>"""


def _meaning_html(text: str) -> str:
    return (text or "").replace("\n", "<br>")


def card_fields(card: Card) -> dict[str, str]:
    return {
        "Word": card.word,
        "Reading": card.reading,
        "Meaning": _meaning_html(card.meaning),
        "Sentence": card.sentence,
        "SentenceMeaning": card.sentence_zh,
        "SentenceAudio": f"[sound:{card.audio}]" if card.audio else "",
        "Picture": f'<img src="{card.image}">' if card.image else "",
        "Source": card.source,
        "Notes": card.note or "",
    }


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def card_guid(card: Card) -> str:
    key = f"{card.word}\x1f{_plain(card.sentence)}".encode("utf-8")
    return base64.b64encode(hashlib.sha1(key).digest()[:10]).decode("ascii")


# ---------------------------------------------------------------- .apkg
def export_apkg(cards: list[Card], out_path: str | Path, deck_name: str) -> int:
    import genanki

    model = genanki.Model(
        MODEL_ID, MODEL_NAME,
        fields=[{"name": f} for f in FIELDS],
        templates=[{"name": "识词", "qfmt": FRONT, "afmt": BACK}],
        css=CSS,
    )
    deck_id = int(hashlib.sha1(deck_name.encode("utf-8")).hexdigest()[:8], 16)
    deck = genanki.Deck(deck_id, deck_name)
    media = []
    for c in cards:
        fields = card_fields(c)
        deck.add_note(genanki.Note(model=model, fields=[fields[f] for f in FIELDS],
                                   guid=card_guid(c), tags=["译声"]))
        for name in (c.audio, c.image):
            if name and (MEDIA_DIR / name).exists():
                media.append(str(MEDIA_DIR / name))
    pkg = genanki.Package(deck)
    pkg.media_files = media
    pkg.write_to_file(str(out_path))
    return len(cards)


# ---------------------------------------------------------------- AnkiConnect
class AnkiConnectError(RuntimeError):
    pass


class AnkiConnect:
    """AnkiConnect 协议客户端：桌面 Anki + AnkiConnect 插件，或手机上的 AnkiConnect Android。"""

    def __init__(self, url: str, timeout: float = 6.0):
        url = (url or "").strip() or "http://127.0.0.1:8765"
        if not url.startswith("http"):
            url = "http://" + url
        if not re.search(r":\d+$", url.rstrip("/")):
            url = url.rstrip("/") + ":8765"
        self.url = url
        self.timeout = timeout

    def call(self, action: str, **params):

        try:
            with make_client(self.url, timeout=self.timeout) as client:
                r = client.post(self.url, json={"action": action, "version": 6, "params": params})
        except httpx.HTTPError as e:
            raise AnkiConnectError("连不上 Anki（Anki 未打开或未安装 AnkiConnect）") from e
        if r.status_code != 200:
            raise AnkiConnectError(f"{self.url} 返回 HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise AnkiConnectError(f"{self.url} 返回的不是 AnkiConnect 数据") from None
        if isinstance(data, dict) and "error" in data:
            if data.get("error"):
                raise AnkiConnectError(str(data["error"]))
            return data.get("result")
        return data

    def version(self) -> int:
        return int(self.call("version"))

    def deck_names(self) -> list[str]:
        return list(self.call("deckNames") or [])

    def ensure_model(self) -> None:
        names = self.call("modelNames") or []
        if MODEL_NAME in names:
            return
        self.call("createModel", modelName=MODEL_NAME, inOrderFields=FIELDS, css=CSS,
                  cardTemplates=[{"Name": "识词", "Front": FRONT, "Back": BACK}])

    def store_media(self, name: str) -> None:
        path = MEDIA_DIR / name
        if name and path.exists():
            self.call("storeMediaFile", filename=name,
                      data=base64.b64encode(path.read_bytes()).decode("ascii"))

    def add_card(self, card: Card, deck: str, model: str = MODEL_NAME,
                 field_map: dict[str, str] | None = None) -> int | None:
        """field_map：自定义笔记类型时，{本工具字段: 你的字段}。"""
        for name in (card.audio, card.image):
            self.store_media(name)
        ours = card_fields(card)
        if field_map:
            fields = {theirs: ours.get(mine, "") for mine, theirs in field_map.items() if theirs}
        else:
            fields = ours
        note = {"deckName": deck, "modelName": model, "fields": fields, "tags": ["译声"],
                "options": {"allowDuplicate": False}}
        return self.call("addNote", note=note)
