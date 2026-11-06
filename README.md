# wisland · Windows 毛玻璃灵动岛

Python 3.12 + PySide6 的 Windows 灵动岛：贴在屏幕边缘的液态毛玻璃胶囊（两侧表面张力圆角、拖动时拉丝断开、吸附回弹），
悬停展开成面板。功能分类参考 [exuupery/island](https://github.com/exuupery/island)。

## 安装

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
copy config.example.json config.json
# 可选：实时日语字幕需要的识别模型（约 630MB，下载到 models/）；加 --dict 同时下载查词词典
.venv\Scripts\python.exe scripts\download_models.py --dict
```

## 启动

- 双击 `启动 wisland.vbs`（无控制台），或在终端运行 `.venv\Scripts\python.exe run.pyw` 看日志
- 托盘图标 / 右键灵动岛 / 导航右侧齿轮 → 编辑配置、重新加载、主题、开机自启、退出
- 日志：`data\wisland.log`

## 交互

| 操作 | 效果 |
|---|---|
| 悬停（或点击）灵动岛 | 弹性展开成面板；移开自动收起 |
| 按住收起态，或按住导航栏空白处并移动 | 拖动灵动岛：液桥被拉长、断开；松手吸附到最近的屏幕边缘 |
| 导航右上角图钉 / 右键菜单「保持展开」 | 灵动岛一直保持展开（仍可拖动） |
| 图钉旁的指针按钮 / 右键菜单「可穿透」 | 鼠标穿过灵动岛，只响应右键（悬停不再展开；热键仍可展开） |
| 收起态滚轮 | 切换收起视图：时间天气 / 小形象 / 音乐（可穿透时也可用） |
| 导航滚轮 | 切换页面 |
| `Ctrl+Alt+I` | 展开 / 收起 |
| `Ctrl+Alt+J` | 翻译剪贴板里的日语（结果显示在灵动岛上） |
| `Ctrl+Alt+V` | 剪贴板历史 |

热键可在 `config.json → hotkeys` 修改（注册失败说明被别的软件占用，日志里会提示）。

## 页面

- **主页**：正在播放（网易云 / B 站 / 浏览器等，经 Windows 媒体会话）、快捷操作、自定义启动器（应用已打开时直接切换过去）
- **消息**：QQ / 微信 / 邮件提醒
- **股票**：自选股行情与涨跌提醒（需要一个本地行情后端，见下文）
- **天气**：OpenWeather，多城市
- **控制**：音量、亮度、WLAN、蓝牙、系统代理、Clash Verge（模式 / 节点 / TUN / 网速）
- **剪贴板**：最近 10 条，点击复制
- **翻译**：日译中文本翻译（DeepSeek）；**实时字幕**：抓取系统声音 → 静音检测断句 → Parakeet 日语识别 → DeepSeek 翻译；
  点字幕里的日语单词查词（Jitendex 中文 / JMdict），一键加入**单词本**（含例句与原声片段），同步到 **Anki**（AnkiConnect）或导出 `.apkg`

## 需要你配置的（`config.json`）

- **天气**：`weather.api_key` 填 OpenWeather 的 key；城市在天气页里用「+」添加、右键删除 / 设为默认（第一个城市显示在收起态）
- **启动器**：`launcher` 数组，每项 `{"name": "...", "path": "C:/.../app.exe", "args": [], "icon": "可选"}`；
  商店应用可写 `"path": "shell:AppsFolder\\<AUMID>"`、`"exe": "进程名.exe"`、`"icon": "appx:<包名>/相对路径.exe"`
- **消息**：`notify.open` 填 QQ / 微信 / Thunderbird 的 exe 路径（点击消息磁贴时打开或切换过去）
- **股票**：`stocks.server` 指向一个本地行情后端，需提供
  `GET /api/watchlist?market=a|hk|us` → `[{code, name}]`、`GET /api/quote?codes=c1,c2` → `{code: {name, price, pct, chg, prev_close, ...}}`、
  `GET /api/minute?code=…`（分时）。`stocks.pct_alert` 涨跌幅提醒阈值（默认 3%，每跨过一个倍数提醒一次）；
  `stocks.alerts` 价格提醒，如 `{"sh600000": {"above": 12, "below": 9}}`
- **翻译 / 实时字幕**：`translate.api_key` 填 DeepSeek API Key；实时字幕的识别参数在 `live.*`（模型目录 `live.models_dir`，默认 `models`）
- **Anki**：`learn.deck` 牌组名；`learn.ankiconnect_url` 留空为本机（需在 Anki 安装 AnkiConnect 插件 2055492159），
  AnkiConnect Android 填 `手机IP:8765`；`learn.auto_sync` 自动同步；`learn.screenshot` + `learn.player_title` 加词时截播放器画面作配图
- **邮件（不开 Thunderbird）**：`mail.imap` 会自动列出 Thunderbird 里的 IMAP 账户，给需要的账户填入 `password`（QQ / 163 用授权码，Gmail 用应用专用密码）。Thunderbird 运行时由它自己提醒。
- **主题**：`island.theme` = `auto`（按身后画面亮度在浅色 / 深色间切换）/ `light` / `dark` / `rouge`（胭脂粉 #FF7CA2 × 墨黑 撞色）/ `sky`（晴空亮蓝 #32ABFD → 柔白浅杏 #F7EFE1 渐变），也可在右键菜单 → 主题里切换

## 说明与限制

- **截图 / 录屏**：默认参与屏幕捕获。毛玻璃取身后画面时只在那约 17ms 内临时把自己排除（静止时每秒约一次），
  所以极少数截图帧里可能缺灵动岛；右键菜单「参与截图」可关闭（关闭后截图里永远看不到灵动岛，但毛玻璃刷新更及时）。
- 微信在托盘时，点微信会发送微信自己的「显示窗口」快捷键（默认 Ctrl+Alt+W，可在 `apps.restore_hotkey` 修改），不会再开新实例。
- 微信没有使用系统通知，只能通过任务栏闪烁检测：微信窗口已打开（或最小化）时可靠，完全隐藏到托盘时可能收不到。
- QQ / Thunderbird 依赖它们弹出的 Windows 通知（请保持系统通知开启）；Thunderbird 另有收件箱文件变化的兜底检测。
- 亮度：直接走 DDC/CI 或 WMI（`brightness.twinkle` 留空；便携版 Twinkle Tray 每次调用都会弹出安装界面）。
- 系统代理开关直接写注册表；如果 Clash Verge 开了「代理守卫」，它可能会把设置改回去。
- 全屏播放时默认不隐藏；需要隐藏可把 `island.hide_fullscreen` 设为 true。

## 致谢

实时字幕的识别 / 翻译流水线（`wisland/live/`）与查词 / 单词本 / Anki（`wisland/learn/`）移植自 [YishengTranslator-White](https://github.com/kayson-39/YishengTranslator-White)
（MIT 许可，见 `wisland/live/LICENSE-YishengTranslator.txt`）。日语识别模型：NVIDIA Parakeet（sherpa-onnx 转换版）；静音检测：Silero VAD。

## 许可

MIT
