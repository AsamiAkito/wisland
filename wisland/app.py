# -*- coding: utf-8 -*-
"""wisland 入口：创建服务、页面、灵动岛，把信号连成"活动"提示。"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import winreg
from pathlib import Path
from types import SimpleNamespace

from PySide6 import QtCore, QtGui, QtWidgets

from .core import bg
from .core.config import CONFIG_PATH, DATA_DIR, ROOT, cfg, expand

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class Actions(QtCore.QObject):
    """跨页面的动作（热键 / 快捷按钮共用）。"""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self._trans_req = -1
        self._trans_show_page = False

    def translate_clipboard(self, show_page: bool = False):
        text = QtGui.QGuiApplication.clipboard().text().strip()
        if not text:
            self.ctx.island.activity({"glyph": _G().TRANSLATE, "title": "剪贴板里没有文字"}, 2.0)
            return
        self.translate_text(text, show_page)

    def translate_text(self, text: str, show_page: bool = False):
        from .ui.theme import G
        text = text.strip()[:4000]
        if not text:
            return
        rid = self.ctx.translate.translate(text)
        self._trans_req = rid
        self._trans_show_page = show_page
        self.ctx.pages["trans"].start(text, rid)
        isl = self.ctx.island
        if show_page and "trans" in isl.nav.keys():
            isl.expand("trans")
        elif not isl.expanded:
            isl.activity({"glyph": G.TRANSLATE, "title": "翻译中…", "sub": text[:30]}, 20)

    def on_translated(self, rid: int, out: str):
        from .ui.theme import G
        isl = self.ctx.island
        if rid != self._trans_req or isl.expanded:
            return
        first = " ".join(out.split())
        isl.activity({"glyph": G.TRANSLATE, "title": first[:60], "sub": ""}, min(12.0, 3.5 + len(first) * 0.12))

    def on_trans_failed(self, rid: int, err: str):
        from .ui.theme import G
        if rid == self._trans_req and not self.ctx.island.expanded:
            self.ctx.island.activity({"glyph": G.TRANSLATE, "title": "翻译失败", "sub": err}, 4)


def _G():
    from .ui.theme import G
    return G


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "wisland")
            return True
    except OSError:
        return False


def _autostart_cmd() -> str:
    """开机自启命令：打包后用 exe 自己；源码运行通过 wisland.vbs（不会弹出终端窗口）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    return f'"{wscript}" "{ROOT / "wisland.vbs"}"'


def fix_autostart():
    """旧版本直接用 .venv/Scripts/pythonw.exe 自启：在 uv 创建的环境里会弹出终端窗口，
    关掉窗口灵动岛也会退出。检测到旧的命令就改成通过 wisland.vbs 启动。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            cur, _ = winreg.QueryValueEx(k, "wisland")
    except OSError:
        return
    if cur != _autostart_cmd():
        try:
            set_autostart(True)
        except OSError:
            pass


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "wisland", 0, winreg.REG_SZ, _autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, "wisland")
            except OSError:
                pass


_QT_TRANSLATORS: list = []


def install_qt_chinese(app: QtWidgets.QApplication) -> bool:
    """让 Qt 自带的界面文字显示中文。

    输入框右键菜单（撤销 / 重做 / 剪切 / 复制 / 粘贴 / 删除 / 全选）以及
    QInputDialog 等标准对话框的按钮，都由 qtbase 翻译提供；不装翻译时 Qt
    会回退到英文。打包后翻译文件在 PySide6/translations 下。
    """
    dirs: list[Path] = []
    try:
        qm_dir = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
    except (AttributeError, RuntimeError):
        qm_dir = ""
    if qm_dir:
        dirs.append(Path(qm_dir))
    try:
        import PySide6
        pkg_dir = Path(PySide6.__file__).resolve().parent / "translations"
        if pkg_dir not in dirs:
            dirs.append(pkg_dir)
    except (ImportError, TypeError):
        pass
    for d in dirs:
        if not (d / "qtbase_zh_CN.qm").is_file():
            continue
        tr = QtCore.QTranslator(app)
        if tr.load("qtbase_zh_CN", str(d)):
            app.installTranslator(tr)
            _QT_TRANSLATORS.append(tr)      # QTranslator 不能交给 GC，否则翻译失效
            print(f"[i18n] 已加载 Qt 中文翻译：{d / 'qtbase_zh_CN.qm'}")
            return True
    print("[i18n] 没找到 qtbase_zh_CN.qm，输入框右键菜单等 Qt 自带文字可能仍是英文")
    return False


def main():
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QtWidgets.QApplication(sys.argv)
    install_qt_chinese(app)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("wisland")
    bg.init()

    lock = QtCore.QLockFile(str(DATA_DIR / "wisland.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("wisland 已在运行")
        return 0
    fix_autostart()

    from .services.audio import AudioService, BrightnessService
    from .services.apps import seed_open_apps
    from .services.clash import ClashService
    from .services.clipboard import ClipboardService
    from .services.dsh import DshService
    from .services.mailcheck import MailCheckService
    from .services.media import MediaService
    from .services.notify import NotifyService
    from .services.stocks import StockService
    from .services.system import RadioService, WindowService
    from .services.lyrics import LyricsService
    from .services.translate import TranslateService
    from .services.weather import WeatherService, weather_glyph
    from .ui.island import Island
    from .ui.pages.home import (HomePage, MessagesPage, enabled_message_modules, module_info)
    from .ui.pages.info import StocksPage, WeatherPage
    from .ui.pages.system import ControlsPage, styled_menu
    from .ui.pages.tools import ClipboardPage, TranslatePage
    from .ui.settings import SECTION_OPTIONS, SettingsWindow
    from .ui.theme import KIND_COLORS, KIND_LIGHT_COLORS, THEME_NAMES, G, theme
    from .ui.brand import kind_pixmap
    from .ui.popup import MessagePopups
    from .ui.widgets import glyph_pixmap

    theme.set_font_family(cfg.get("island.font_family", ""))
    theme.set_mode(cfg.get("island.theme", "auto"))
    theme.refresh_fonts()               # 让 Qt 自带的提示框 / 右键菜单也用上自定义字体
    ctx = SimpleNamespace()
    ctx.media = MediaService()
    ctx.audio = AudioService()
    ctx.brightness = BrightnessService()
    ctx.radio = RadioService()
    ctx.windows = WindowService()
    ctx.notify = NotifyService()
    ctx.dsh = DshService()
    ctx.stocks = StockService()
    ctx.weather = WeatherService()
    ctx.clash = ClashService()
    ctx.translate = TranslateService()
    ctx.lyrics = LyricsService()
    ctx.clipboard = ClipboardService()
    ctx.mailcheck = MailCheckService()
    from .live.service import LiveService
    ctx.live = LiveService()
    from .learn.service import LearnService
    ctx.learn = LearnService()
    ctx.actions = Actions(ctx)
    ctx.notify.opener = lambda path: ctx.windows.open_or_focus(path)
    ctx.windows.notice.connect(lambda t: ctx.island and ctx.island.activity({"glyph": _G().MESSAGE, "title": t}, 5.0))
    ctx.island = None

    # 每次启动读取本地常见程序，填进「消息与邮件 → 打开应用」中仍为空的项。
    try:
        seed_open_apps()
    except Exception as e:  # noqa: BLE001
        print("[notify] 初始化打开应用路径失败：", e)

    pages = [HomePage(ctx), MessagesPage(ctx), StocksPage(ctx), WeatherPage(ctx), ControlsPage(ctx),
             ClipboardPage(ctx), TranslatePage(ctx)]
    ctx.pages = {p.key: p for p in pages}
    P = ctx.pages

    SECTIONS = [(k, label) for k, label, _glyph in SECTION_OPTIONS]
    SECTION_KEYS = {k for k, _ in SECTIONS}

    def hidden_tabs() -> list:
        v = cfg.get("island.hidden_tabs")
        return [k for k in v if isinstance(k, str)] if isinstance(v, list) else []

    def page_on(key: str) -> bool:
        """板块是否显示在灵动岛里（主页与消息固定显示）。"""
        return key not in SECTION_KEYS or key not in hidden_tabs()

    def peak():
        try:
            return ctx.audio.peak()
        except Exception:  # noqa: BLE001
            return 0.0

    island = Island(pages, peak_fn=peak, visible_fn=page_on)
    ctx.island = island
    compact = island.compact
    popups = MessagePopups()
    ctx.popups = popups
    app.aboutToQuit.connect(popups.close_all)

    # ---------------- 媒体 ----------------
    media_st = {"key": None, "lyr": None, "prev_view": None, "playing": False}
    back_timer = QtCore.QTimer(app, singleShot=True, interval=8000)

    def update_lyrics(m):
        k = ctx.lyrics.key(m) if m.get("title") else None
        if k != media_st["lyr"]:
            media_st["lyr"] = k
            compact.set_lyrics(None)
        if k:
            lyr = ctx.lyrics.get(m)
            if lyr is not None and compact.lyrics is not lyr:
                compact.set_lyrics(lyr)

    def on_lyrics_ready(k):
        if k == media_st["lyr"]:
            update_lyrics(compact.media)
            island._kick()
    ctx.lyrics.ready.connect(on_lyrics_ready)

    def back_to_view():
        # 暂停一段时间后回到原来的收起视图（时间天气 / 小形象）
        if not compact.playing() and media_st["prev_view"] and compact.view == "music":
            compact.set_view(media_st["prev_view"])
            island._kick()
        media_st["prev_view"] = None
    back_timer.timeout.connect(back_to_view)

    def on_media(m):
        compact.set_media(m)
        P["home"].media.set_media(m)
        update_lyrics(m)
        playing = bool(m.get("playing") and m.get("title"))
        key = (m.get("app_id"), m.get("title"), m.get("artist"))
        if cfg.get("media.auto_show", True):
            if playing and (not media_st["playing"] or key != media_st["key"]):
                back_timer.stop()
                if compact.view in ("clock", "pet"):
                    media_st["prev_view"] = compact.view
                    compact.set_view("music")
            elif not playing and media_st["playing"] and media_st["prev_view"]:
                back_timer.start()
        media_st["playing"], media_st["key"] = playing, key
        island._kick()
    ctx.media.changed.connect(on_media)

    def on_cover(img):
        compact.set_cover(img)
        P["home"].media.set_cover(img)
        island._kick()
    ctx.media.cover_changed.connect(on_cover)

    # ---------------- 音量（外部改动也弹出提示，像手机一样） ----------------
    vol_state = {"init": True, "last": None}

    def on_volume(v, muted):
        P["ctl"].set_volume(v, muted)
        P["home"].set_muted(muted)
        key = (round(v, 3), muted)
        if vol_state["init"]:
            vol_state["init"] = False
        elif key != vol_state["last"] and not island.expanded:
            act = {"type": "volume", "glyph": G.MUTE if muted else G.VOLUME, "value": v, "muted": muted}
            if compact.activity and compact.activity.get("type") == "volume":
                compact.update_activity(**act, until=time.monotonic() + 1.8)
            else:
                island.activity(act, 1.8)
            island._kick()
        vol_state["last"] = key
    ctx.audio.volume_changed.connect(on_volume)
    compact.set_view(cfg.get("island.compact_view", "clock"))

    def on_wheel(d):
        v = compact.cycle_view(d)
        cfg.set("island.compact_view", v)
        island._kick()
    island.wheel_compact.connect(on_wheel)
    ctx.brightness.changed.connect(P["ctl"].set_brightness)
    ctx.radio.changed.connect(P["ctl"].set_radio)

    # ---------------- 收起态：时间天气与小形象每隔几十秒自动轮换 ----------------
    def auto_cycle():
        if island.expanded or island.passthrough() or compact.view not in ("clock", "pet"):
            return
        compact.set_view("pet" if compact.view == "clock" else "clock")
        island._kick()

    cyc_timer = QtCore.QTimer(app)
    cyc_timer.timeout.connect(auto_cycle)

    def restart_cycle():
        cyc_timer.stop()
        try:
            cyc = int(cfg.get("island.auto_cycle_s", 30) or 0)
        except (TypeError, ValueError):
            cyc = 30
        if cyc > 0:
            cyc_timer.start(cyc * 1000)
    restart_cycle()

    # ---------------- Clash ----------------
    clash_state = {"mode": None}

    def on_clash(d):
        P["ctl"].set_clash(d)
        m = d.get("mode")
        if d.get("ok") and clash_state["mode"] and m != clash_state["mode"] and not island.expanded:
            names = {"rule": "规则模式", "global": "全局模式", "direct": "直连模式"}
            island.activity({"glyph": G.GLOBE, "title": "Clash", "sub": names.get(m, m)}, 2.2)
        if d.get("ok"):
            clash_state["mode"] = m
    ctx.clash.updated.connect(on_clash)


    # ---------------- 消息提醒 ----------------
    def on_notified(n):
        kind = n.get("kind", "other")
        glyph = module_info(kind)[1]
        body = (n.get("body") or "").strip()
        sender = (n.get("sender") or "").strip()
        if body:
            # 正文预览（飞书 / 钉钉等）：有发送者就用发送者做标题，正文做副标题
            title = sender or n.get("name", kind)
            sub = body
        else:
            title = n.get("name", kind)
            sub = "新邮件" if kind == "mail" else (sender or "新消息")
        compact.pet.react("surprise")
        P["home"].pet.face.react("surprise")
        compact.idle_order = [kind] + [k for k in getattr(compact, "idle_order", []) if k != kind]
        glow = KIND_LIGHT_COLORS.get(kind)
        if glow is not None:
            island.breath(glow, 5.0)
        popups.show(kind, title, sub, island.scr)
        island.activity({"glyph": glyph, "color": KIND_COLORS.get(kind, KIND_COLORS["other"]),
                         "icon": kind_pixmap(kind),
                         "title": title, "sub": sub}, 4.0)
        if P["msg"].active:
            P["msg"].refresh()

    def on_counts(c):
        kinds = set(enabled_message_modules())
        c = {k: v for k, v in c.items() if k in kinds}
        compact.badges = dict(c)
        island.nav.set_badge("msg", sum(c.values()))
        if P["msg"].active:
            P["msg"].refresh()
        island._kick()
    ctx.notify.notified.connect(on_notified)
    ctx.notify.counts_changed.connect(on_counts)
    ctx.dsh.notified.connect(ctx.notify.publish)
    island.flash.connect(ctx.notify.on_flash)
    ctx.mailcheck.new_mail.connect(lambda user, n: ctx.notify._emit(
        {"kind": "mail", "name": "邮件", "source": "imap", "time": time.time()}))

    # ---------------- 股票 ----------------
    ctx.stocks.updated.connect(P["stock"].set_data)

    def on_stock_alert(a):
        if not page_on("stock"):
            return                       # 板块已隐藏：不再在收起态弹出股票提醒
        pct = a.get("pct") or 0
        col = theme.up() if pct > 0 else theme.down()
        island.activity({"glyph": G.STOCK, "color": col, "title": a.get("name", a.get("code", "")),
                         "sub": a.get("text", ""), "sub_color": col}, 5.0)
    ctx.stocks.alert.connect(on_stock_alert)

    # ---------------- 天气 ----------------
    def on_weather(d):
        P["weather"].set_data(d)
        if d.get("ok") or d.get("temp") is not None:
            g, fam = weather_glyph(d.get("icon"))
            compact.weather = (g, f"{round(d.get('temp', 0))}°", fam)
        island._kick()
    ctx.weather.updated.connect(on_weather)

    # ---------------- 翻译 ----------------
    ctx.translate.finished.connect(ctx.actions.on_translated)
    ctx.translate.failed.connect(ctx.actions.on_trans_failed)

    # ---------------- 实时字幕：收起态显示最新一句 ----------------
    # 收起态两行显示：上面日语、下面中文；有人说话时优先显示，安静几秒后回到原视图（不打断音乐）
    sub_st = {"sid": None}

    def on_partial(jp):
        if jp:
            compact.set_sub(jp=jp)
            island._kick()

    def on_segment(sid, jp):
        if sid != sub_st["sid"]:
            sub_st["sid"] = sid
        compact.set_sub(jp=jp)
        island._kick()

    def on_zh(sid, zh):
        if zh:
            compact.set_sub(zh=zh)
            island._kick()

    def on_running(on):
        compact.set_sub_running(on)
        island._kick()
    ctx.live.partial.connect(on_partial)
    ctx.live.segment.connect(on_segment)
    ctx.live.live_zh.connect(on_zh)
    ctx.live.final_zh.connect(on_zh)
    ctx.live.running_changed.connect(on_running)
    island.play_pause_requested.connect(lambda: ctx.media.play_pause())
    ctx.learn.notice.connect(lambda t: island.activity({"glyph": _G().CHECK, "title": t}, 2.6))
    ctx.live.error.connect(lambda m: island.activity({"glyph": G.LIVE, "title": "实时字幕", "sub": m}, 4))

    # ---------------- 热键 ----------------
    def on_hotkey(name):
        if name == "toggle":
            island.toggle()
        elif name == "translate":
            ctx.actions.translate_clipboard()
        elif name == "clipboard":
            if page_on("clip"):
                island.toggle("clip")
            else:
                island.activity({"glyph": G.CLIP, "title": "剪贴板板块已关闭",
                                 "sub": "右键 → 显示板块 可开启"}, 3.0)
    island.hotkey.connect(on_hotkey)

    # ---------------- 菜单 / 托盘 ----------------
    def set_section_show(key: str, label: str, on: bool):
        """显示 / 隐藏某个板块（立即生效；股票会同时启停行情轮询）。"""
        hidden = [k for k in hidden_tabs() if k != key]
        if not on:
            hidden.append(key)
        cfg.set("island.hidden_tabs", hidden)
        island.apply_page_visibility()
        if key == "stock":
            if on:
                ctx.stocks.start()
            else:
                ctx.stocks.stop()
        pg = next((p for p in island.all_pages if p.key == key), None)
        glyph = pg.glyph if pg is not None else G.APPS
        island.activity({"glyph": glyph, "title": f"已{'显示' if on else '隐藏'}{label}板块"}, 2.0)

    def toggle_section(key: str, label: str):
        set_section_show(key, label, not page_on(key))

    def _sig(v) -> str:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)

    def _cfg_state(d: dict) -> dict:
        """从一份配置数据里取出 reload_cfg 需要比较的几项（设置窗口保存前会传旧的 data 进来）。"""
        isl = d.get("island") if isinstance(d.get("island"), dict) else {}
        mode = isl.get("expand_mode")
        if mode not in ("hover", "always", "never"):
            mode = "always" if isl.get("pinned", False) else "hover"

        def box(name: str) -> dict:
            v = d.get(name)
            return v if isinstance(v, dict) else {}
        mail, clash, weather = box("mail"), box("clash"), box("weather")
        media = box("media")
        return {
            "expand": mode,
            "keep_on_top": bool(isl.get("keep_on_top", True)),
            "passthrough": bool(isl.get("passthrough", False)),
            "compact": isl.get("compact_view", "clock"),
            "font": str(isl.get("font_family") or ""),
            "mail": _sig([mail.get("interval_s"), mail.get("imap")]),
            "clash": _sig([clash.get("config_dir"), clash.get("app")]),
            "weather": _sig([weather.get("api_key"), weather.get("units"), weather.get("lang"),
                             weather.get("interval_min"), weather.get("cities")]),
            "lyrics": _sig([media.get("lyrics"), media.get("lyrics_sources"),
                            media.get("lyrics_custom_url")]),
        }

    def reload_cfg(prev_data=None):
        """从磁盘重新读取配置并尽量立即生效（设置窗口保存后也走这里）。

        prev_data：保存前的 cfg.data。设置窗口在写盘前先改了内存里的 cfg.data，
        所以不能再用它当"保存前"的快照，由调用方传进来。
        """
        prev = _cfg_state(prev_data if isinstance(prev_data, dict) else cfg.data)
        cfg.load()
        now = _cfg_state(cfg.data)
        if now["font"] != prev["font"]:
            theme.set_font_family(now["font"])
            theme.refresh_fonts()
        theme.set_mode(cfg.get("island.theme", "auto"))
        island.apply_layout_settings()
        island.register_hotkeys()
        island.apply_capture_mode()
        if now["expand"] != prev["expand"]:
            island.set_expand_mode(now["expand"])
        if now["keep_on_top"] != prev["keep_on_top"]:
            island.apply_keep_on_top()
        if now["passthrough"] != prev["passthrough"]:
            island.set_passthrough(now["passthrough"])
        if now["compact"] != prev["compact"]:
            compact.set_view(now["compact"])
            island._kick()
        P["home"].build_launcher()
        P["msg"].sync_modules()
        if now["weather"] != prev["weather"]:
            ctx.weather.stop()
            ctx.weather.start()
        if now["lyrics"] != prev["lyrics"]:
            ctx.lyrics.clear()
            media_st["lyr"] = None
        island.apply_page_visibility()
        if page_on("stock"):
            ctx.stocks.start()
        else:
            ctx.stocks.stop()
        if now["mail"] != prev["mail"]:
            ctx.mailcheck.stop()
            ctx.mailcheck.start()
        if now["clash"] != prev["clash"]:
            ctx.clash.stop()
            QtCore.QTimer.singleShot(600, ctx.clash.start)
        restart_cycle()
        island.activity({"glyph": G.CHECK, "title": "配置已重新加载"}, 1.8)

    _settings = {"win": None}

    def open_settings():
        """可视化设置窗口；已经打开时只把它带到前面。"""
        win = _settings["win"]
        if win is not None:
            try:
                if win.isVisible():
                    win.raise_()
                    win.activateWindow()
                    return
                win.close()
                win.deleteLater()
            except RuntimeError:
                pass
            _settings["win"] = None
        win = SettingsWindow(on_apply=reload_cfg)
        _settings["win"] = win

        def on_closed():
            if _settings["win"] is win:
                _settings["win"] = None
        win.closed.connect(on_closed)
        win.show()
        win.raise_()
        win.activateWindow()

    def build_menu(menu: QtWidgets.QMenu):
        menu.addAction("设置…").triggered.connect(open_settings)
        menu.addAction("打开 config.json").triggered.connect(lambda: os.startfile(str(CONFIG_PATH)))  # noqa: S606
        menu.addAction("重新加载配置").triggered.connect(reload_cfg)
        menu.addSeparator()
        tm = menu.addMenu("主题")
        for mode, label in THEME_NAMES:
            a = tm.addAction(("✓  " if cfg.get("island.theme") == mode else "    ") + label)
            a.triggered.connect(lambda _=False, m=mode: (cfg.set("island.theme", m), theme.set_mode(m)))
        em = menu.addMenu("展开方式")
        for mode, label in (("hover", "悬停展开"), ("always", "常驻展开"), ("never", "长关")):
            a = em.addAction(("✓  " if island.expand_mode() == mode else "    ") + label)
            a.triggered.connect(lambda _=False, m=mode: island.set_expand_mode(m))
        sm = menu.addMenu("显示板块")
        for key, label in SECTIONS:
            a = sm.addAction(("✓  " if page_on(key) else "    ") + label)
            a.triggered.connect(lambda _=False, k=key, l=label: toggle_section(k, l))
        a = menu.addAction(("✓  " if island.passthrough() else "    ") + "可穿透")
        a.triggered.connect(lambda: island.set_passthrough(not island.passthrough()))
        a = menu.addAction(("✓  " if island.capture_visible() else "    ") + "参与截图")
        a.triggered.connect(lambda: island.set_capture_visible(not island.capture_visible()))
        a = menu.addAction(("✓  " if autostart_enabled() else "    ") + "开机自启")
        a.triggered.connect(lambda: set_autostart(not autostart_enabled()))
        menu.addAction("打开数据目录").triggered.connect(lambda: os.startfile(str(DATA_DIR)))  # noqa: S606
        menu.addSeparator()
        menu.addAction("退出 wisland").triggered.connect(app.quit)

    def show_menu(pos):
        m = styled_menu(island)
        build_menu(m)
        island.locks.add("menu")
        m.aboutToHide.connect(lambda: island.locks.discard("menu"))
        m.popup(pos)
    island.menu_requested.connect(show_menu)
    # 导航：「主页」标签放在播放器封面正上方
    def nav_anchor():
        m = P["home"].media
        if m.width() < 10:
            return None
        g = m.mapToGlobal(QtCore.QPoint(m.width() // 2, 0))
        return island.nav.mapFromGlobal(g).x()
    island.nav.anchor_fn = nav_anchor

    # 主页的「保持展开」「可穿透」按钮
    home = P["home"]
    MODE_UI = {"hover": (G.EYE, "悬停展开"), "always": (G.PIN, "常驻展开"), "never": (G.EYE_OFF, "长关")}
    NEXT = {"hover": "always", "always": "never", "never": "hover"}
    # 按钮只在 常驻展开 ↔ 悬停展开 之间切换；长关用双击灵动岛切换
    home.a_pin.clicked.connect(lambda: island.set_expand_mode("hover" if island.expand_mode() == "always" else "always"))
    home.a_pt.clicked.connect(lambda: island.set_passthrough(not island.passthrough()))

    def on_mode(mode):
        # 按钮只表示 常驻展开 / 悬停展开；长关不在按钮上显示
        if mode == "never":
            mode = "hover"
        g, label = MODE_UI.get(mode, MODE_UI["hover"])
        home.a_pin.set_on(mode == "always", g, label)

    def on_pt(on):
        home.a_pt.set_on(on, None, "可穿透")
        if on:
            compact.set_view("pet")            # 穿透时直接显示小形象
            island._kick()
    island.mode_changed.connect(on_mode)
    island.passthrough_changed.connect(on_pt)
    on_mode(island.expand_mode())
    home.a_pt.set_on(island.passthrough(), None, "可穿透")
    if island.passthrough():
        compact.set_view("pet")
    island.nav.add_right_button(G.GEAR, "设置", open_settings)

    tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(glyph_pixmap(G.APPS, 32, QtGui.QColor("#4f8cff"))), app)
    tray.setToolTip("wisland 灵动岛")
    tmenu = QtWidgets.QMenu()
    tmenu.aboutToShow.connect(lambda: (tmenu.clear(), tmenu.addAction("展开 / 收起").triggered.connect(island.toggle),
                                       build_menu(tmenu)))
    tray.setContextMenu(tmenu)
    tray.activated.connect(lambda r: island.toggle() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # ---------------- 全屏时隐藏 ----------------
    def check_fullscreen():
        if not cfg.get("island.hide_fullscreen", True):
            island.set_fullscreen_hidden(False)
            return
        try:
            island.set_fullscreen_hidden(ctx.windows.foreground_is_fullscreen(int(island.winId())))
        except Exception:  # noqa: BLE001
            pass
    fs_timer = QtCore.QTimer(app, interval=1000, timeout=check_fullscreen)

    # ---------------- 启动 ----------------
    island.show()
    for s in (ctx.media, ctx.audio, ctx.brightness, ctx.radio, ctx.windows, ctx.notify, ctx.dsh, ctx.stocks,
              ctx.weather, ctx.clash, ctx.translate, ctx.clipboard, ctx.mailcheck, ctx.learn):
        try:
            if not hasattr(s, "start"):
                continue
            if s is ctx.stocks and not page_on("stock"):
                continue                    # 股票板块隐藏时不轮询行情
            s.start()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    fs_timer.start()

    def shutdown():
        ctx.live.stop(wait=True)
        for s in (ctx.media, ctx.audio, ctx.brightness, ctx.radio, ctx.notify, ctx.dsh, ctx.stocks,
                  ctx.weather, ctx.clash, ctx.translate, ctx.clipboard, ctx.mailcheck, ctx.learn):
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                pass
        cfg.save()
        lock.unlock()
    app.aboutToQuit.connect(shutdown)
    return app.exec()
