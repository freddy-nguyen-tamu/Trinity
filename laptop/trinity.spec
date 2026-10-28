# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['trinity.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'googleapiclient.discovery',
        'google.oauth2.service_account',
        'google.oauth2.credentials',
        'google.auth.transport.requests',
        'google_auth_oauthlib.flow',
    ],
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
    name='trinity',
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
