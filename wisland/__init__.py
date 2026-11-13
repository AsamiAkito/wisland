# -*- coding: utf-8 -*-
"""wisland 的版本与身份常量。

这里刻意只放字符串常量、不导入 Qt 或任何重依赖：设置窗口、更新服务、构建脚本都会读取它，
保持轻量可以避免循环导入。发布新版本时，同步更新仓库根目录的 VERSION 文件。
"""
from __future__ import annotations

__version__ = "1.6.5"

APP_NAME = "Wisland"
APP_NAME_CN = "wisland 液态灵动岛"
APP_TAGLINE = "贴在屏幕边缘的液态毛玻璃胶囊"

AUTHOR_NAME = "白木"
AUTHOR_URL = "https://gl.baimu.live/"

REPO_URL = "https://cnb.cool/shiroki.live/wisland-zus"

LICENSE_NAME = "MIT License"
LICENSE_URL = REPO_URL + "/-/blob/main/LICENSE"

# 原创项目（wisland 的最初版本）
ORIGINAL_AUTHOR_NAME = "AsamiAkito"
ORIGINAL_REPO_URL = "https://github.com/AsamiAkito/wisland"
ORIGINAL_LICENSE_NAME = "MIT License"
ORIGINAL_LICENSE_URL = ORIGINAL_REPO_URL + "/blob/main/LICENSE"
