# -*- mode: python ; coding: utf-8 -*-
# 云手机后端打包配置（PyInstaller）
# 运行：.venv\Scripts\pyinstaller cloud-phone-backend.spec --noconfirm --clean
#
# datas 说明：
#   app        -> 后端全部源码
#   bin        -> ffmpeg.exe（fMP4 视频流转封装，stream.py 按 _MEIPASS/bin 探测）
#   resources  -> resources/apk（新容器初始化 iLauncher / xBrowser，orchestrator 按 _MEIPASS/resources 探测）
# 注意：instance 不再捆绑——运行时数据库路径由 Electron 主进程以 DATABASE_URL 环境变量注入
#       （默认 %APPDATA%\CloudPhoneClient\cloud_phone.db），避免写入 PyInstaller 临时目录导致重启丢数据。

a = Analysis(
    ['run_stable.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app', 'app'),
        ('bin', 'bin'),
        ('resources', 'resources'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='cloud-phone-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
