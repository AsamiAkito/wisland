# Wisland · Windows 液态灵动岛

贴在屏幕边缘的液态毛玻璃胶囊，悬停弹性展开。Windows 10 / 11，Python + PySide6。

<p align="center"><img src="docs/home.png" width="720"></p>

## 安装

1. 安装 **Python 3.10 或更新版本**：<https://www.python.org/downloads/windows/>
   （安装时勾选 **Add python.exe to PATH**）
2. 下载本项目：右侧 **Releases** 里下载 `wisland-x.y.z.zip` 并解压
   （或 `git clone https://github.com/AsamiAkito/wisland.git`）。
   路径里最好不要有特殊字符。
3. 双击 **`setup.bat`**：自动创建 `.venv`、补齐 pip、安装依赖（PyPI 连不上时自动改用清华镜像），需要几分钟。
4. 双击 **`wisland.vbs`** 启动。还没运行过 `setup.bat` 时，它会提示并帮你运行。

开机自启：右键灵动岛 → 开机自启。

## 功能

- **液态外形**：与屏幕边缘以表面张力相连；按住拖动会拉丝、断开，松手吸附到最近的边缘
- **收起态**：时间天气 / 小形象 / 正在播放，滚轮切换；开始播放音乐时自动显示**滚动歌词**；新消息、音量、股票提醒以动画浮现
- **小形象**：发光双眼，跟随鼠标，靠近时随机变换表情；按压会被挤歪
- **音乐**：网易云音乐（直接读取本地播放日志，无需插件）、QQ 音乐、Spotify、B 站 / 浏览器等；歌词来自网易云公开歌词（含翻译）
- **主页**：播放控制、展开方式、可穿透、静音、实时字幕、应用启动器
- **消息**：QQ / 微信 / 邮件 提醒（读取 Windows 通知与任务栏闪烁，不读消息内容）
- **股票**：A 股 / 港股 / 美股行情与分时（腾讯财经公开接口），点 + 搜索添加，右键删除
- **天气**：多城市，逐小时与 5 天预报（默认 Open-Meteo，无需 Key）
- **控制**：音量、亮度、WLAN、蓝牙；检测到 Clash Verge 时显示代理控制
- **剪贴板**：最近 10 条文字 / 图片，点击复制
- **翻译**：日译中；实时字幕（系统声音 → 日语识别 → 翻译），点词查词，加入单词本并同步到 Anki
- **主题**：深色 / 浅色 / 胭脂撞色 / 晴空渐变

操作：悬停展开，鼠标离开收起；**双击**灵动岛切换「长关」（不再自动展开）；右键打开菜单（主题、展开方式、可穿透、编辑配置等）。

## 可选设置

配置文件是 `config.json`（首次启动自动生成；右键灵动岛 → 编辑配置，改完后右键 → 重新加载配置）。

| 功能 | 需要什么 |
| --- | --- |
| 翻译、实时字幕的中文翻译 | DeepSeek API Key → `translate.api_key` |
| 实时字幕的日语识别 | 下载模型（约 630 MB）：`.venv\Scripts\python.exe scripts\download_models.py`（国内网络可先 `set HF_ENDPOINT=https://hf-mirror.com`） |
| 点词查词的词典 | `.venv\Scripts\python.exe scripts\download_models.py --dict` |
| 天气用 OpenWeather | `weather.api_key`（留空则使用 Open-Meteo） |
| 自选股 | 股票页点 **+** 搜索添加，或编辑 `stocks.watchlist` |
| 邮件未读提醒（不开 Thunderbird） | `mail.imap` 里填 IMAP 授权码 |
| 同步到 Anki | 安装 Anki 插件 AnkiConnect |

## 常见问题

- **双击 vbs 报错 80070002 / 没反应**：还没安装运行环境，先运行 `setup.bat`。日志在 `data\wisland.log`。
- **setup.bat 提示找不到 Python**：装 Python 时没勾选 Add to PATH；重新运行安装程序选 Modify 勾上即可。
- **没显示正在播放 / 没有歌词**：网易云无需设置（读取它的本地日志）；QQ 音乐需在 设置 → 常规设置 中勾选「显示系统媒体传输控制(SMTC)」。没有接入系统媒体控件的播放器，播放按钮会改发键盘媒体键。周杰伦等网易云没有版权的歌曲没有歌词。
- **不想自动切到音乐 / 不要歌词**：`config.json → media.auto_show` / `media.lyrics` 改为 false。
- **蓝牙磁贴是灰色**：电脑没有蓝牙适配器或驱动不支持系统开关，点击会打开系统蓝牙设置。
- **热键无效**：可能被其他软件占用，在 `config.json → hotkeys` 里改。

## 效果图

<p align="center">
<img src="docs/compact_clock.png" height="56">&nbsp;
<img src="docs/compact_music.png" height="56">&nbsp;
<img src="docs/compact_pet.png" height="56">
</p>

<p align="center"><img src="docs/expressions.png" width="860"></p>

<p align="center"><img src="docs/subtitles.png" width="600"></p>

<p align="center">
<img src="docs/theme_light.png" width="280">
<img src="docs/theme_rouge.png" width="280">
<img src="docs/theme_sky.png" width="280">
</p>

---

实时字幕与单词本流水线移植自 [YishengTranslator-White](https://github.com/kayson-39/YishengTranslator-White)（MIT）。MIT License.
