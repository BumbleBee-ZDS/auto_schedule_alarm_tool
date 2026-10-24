# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['monitor_gui.py'],
    pathex=[],
    binaries=[('G:/SoftWare/miniconda/Library/bin/tcl86t.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/tk86t.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/ffi-8.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/ffi.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/libcrypto-3-x64.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/libssl-3-x64.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/libmpdec-4.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/libexpat.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/sqlite3.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/liblzma.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/libbz2.dll', '.'), ('G:/SoftWare/miniconda/Library/bin/zlib.dll', '.')],
    datas=[],
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
    name='DataMonitorDebug',
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
