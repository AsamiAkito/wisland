# -*- coding: utf-8 -*-
"""wisland 入口：创建服务、页面、灵动岛，把信号连成"活动"提示。"""
from __future__ import annotations

import os
import sys
import time
import traceback
import winreg
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
        if show_page:
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


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
            winreg.SetValueEx(k, "wisland", 0, winreg.REG_SZ, f'"{pyw}" "{ROOT / "run.pyw"}"')
        else:
            try:
                winreg.DeleteValue(k, "wisland")
            except OSError:
                pass


def main():
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("wisland")
    bg.init()

    lock = QtCore.QLockFile(str(DATA_DIR / "wisland.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("wisland 已在运行")
        return 0

    from .services.audio import AudioService, BrightnessService
    from .services.clash import ClashService
    from .services.clipboard import ClipboardService
    from .services.mailcheck import MailCheckService
    from .services.media import MediaService
    from .services.notify import NotifyService
    from .services.stocks import StockService
    from .services.system import RadioService, WindowService
    from .services.lyrics import LyricsService
    from .services.translate import TranslateService
    from .services.weather import WeatherService, weather_glyph
    from .ui.island import Island
    from .ui.pages.home import HomePage, MessagesPage
    from .ui.pages.info import StocksPage, WeatherPage
    from .ui.pages.system import ControlsPage, styled_menu
    from .ui.pages.tools import ClipboardPage, TranslatePage
    from .ui.theme import KIND_COLORS, THEME_NAMES, G, theme
    from .ui.widgets import glyph_pixmap

    theme.set_mode(cfg.get("island.theme", "auto"))
    ctx = SimpleNamespace()
    ctx.media = MediaService()
    ctx.audio = AudioService()
    ctx.brightness = BrightnessService()
    ctx.radio = RadioService()
    ctx.windows = WindowService()
    ctx.notify = NotifyService()
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

    pages = [HomePage(ctx), MessagesPage(ctx), StocksPage(ctx), WeatherPage(ctx), ControlsPage(ctx),
             ClipboardPage(ctx), TranslatePage(ctx)]
    ctx.pages = {p.key: p for p in pages}
    P = ctx.pages

    def peak():
        try:
            return ctx.audio.peak()
        except Exception:  # noqa: BLE001
            return 0.0

    island = Island(pages, peak_fn=peak)
    ctx.island = island
    compact = island.compact

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
        glyph = {"mail": G.MAIL, "qq": G.CHAT, "wechat": G.MESSAGE}.get(kind, G.BELL)
        sub = "新邮件" if kind == "mail" else (n.get("sender") or "新消息")
        compact.pet.react("surprise")
        P["home"].pet.face.react("surprise")
        island.activity({"glyph": glyph, "color": KIND_COLORS.get(kind, KIND_COLORS["other"]),
                         "title": n.get("name", kind), "sub": sub}, 4.0)
        if P["msg"].active:
            P["msg"].refresh()

    def on_counts(c):
        compact.badges = dict(c)
        island.nav.set_badge("msg", sum(c.values()))
        if P["msg"].active:
            P["msg"].refresh()
        island._kick()
    ctx.notify.notified.connect(on_notified)
    ctx.notify.counts_changed.connect(on_counts)
    island.flash.connect(ctx.notify.on_flash)
    ctx.mailcheck.new_mail.connect(lambda user, n: ctx.notify._emit(
        {"kind": "mail", "name": "邮件", "source": "imap", "time": time.time()}))

    # ---------------- 股票 ----------------
    ctx.stocks.updated.connect(P["stock"].set_data)

    def on_stock_alert(a):
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
            island.toggle("clip")
    island.hotkey.connect(on_hotkey)

    # ---------------- 菜单 / 托盘 ----------------
    def reload_cfg():
        cfg.load()
        theme.set_mode(cfg.get("island.theme", "auto"))
        island.register_hotkeys()
        P["home"].build_launcher()
        ctx.weather.refresh()
        island.activity({"glyph": G.CHECK, "title": "配置已重新加载"}, 1.8)

    def build_menu(menu: QtWidgets.QMenu):
        menu.addAction("编辑配置 config.json").triggered.connect(lambda: os.startfile(str(CONFIG_PATH)))  # noqa: S606
        menu.addAction("重新加载配置").triggered.connect(reload_cfg)
        tm = menu.addMenu("主题")
        for mode, label in THEME_NAMES:
            a = tm.addAction(("✓  " if cfg.get("island.theme") == mode else "    ") + label)
            a.triggered.connect(lambda _=False, m=mode: (cfg.set("island.theme", m), theme.set_mode(m)))
        em = menu.addMenu("展开方式")
        for mode, label in (("hover", "悬停展开"), ("always", "常驻展开"), ("never", "长关")):
            a = em.addAction(("✓  " if island.expand_mode() == mode else "    ") + label)
            a.triggered.connect(lambda _=False, m=mode: island.set_expand_mode(m))
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
    island.nav.add_right_button(G.GEAR, "设置", lambda: show_menu(QtGui.QCursor.pos()))

    tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(glyph_pixmap(G.APPS, 32, QtGui.QColor("#4f8cff"))), app)
    tray.setToolTip("wisland 灵动岛")
    tmenu = QtWidgets.QMenu()
    tmenu.aboutToShow.connect(lambda: (tmenu.clear(), tmenu.addAction("展开 / 收起").triggered.connect(island.toggle),
                                       build_menu(tmenu)))
    tray.setContextMenu(tmenu)
    tray.activated.connect(lambda r: island.toggle() if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()

    # ---------------- 收起态：时间天气与小形象每隔几十秒自动轮换 ----------------
    def auto_cycle():
        if island.expanded or island.passthrough() or compact.view not in ("clock", "pet"):
            return
        compact.set_view("pet" if compact.view == "clock" else "clock")
        island._kick()
    cyc = int(cfg.get("island.auto_cycle_s", 30) or 0)
    if cyc > 0:
        QtCore.QTimer(app, interval=cyc * 1000, timeout=auto_cycle).start()

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
    for s in (ctx.media, ctx.audio, ctx.brightness, ctx.radio, ctx.windows, ctx.notify, ctx.stocks,
              ctx.weather, ctx.clash, ctx.translate, ctx.clipboard, ctx.mailcheck, ctx.learn):
        try:
            if hasattr(s, "start"):
                s.start()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    fs_timer.start()

    def shutdown():
        ctx.live.stop(wait=True)
        for s in (ctx.media, ctx.audio, ctx.brightness, ctx.radio, ctx.notify, ctx.stocks,
                  ctx.weather, ctx.clash, ctx.translate, ctx.clipboard, ctx.mailcheck, ctx.learn):
            try:
                s.stop()
            except Exception:  # noqa: BLE001
                pass
        cfg.save()
        lock.unlock()
    app.aboutToQuit.connect(shutdown)
    return app.exec()
