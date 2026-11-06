# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_dynamic_libs

block_cipher = None

ROOT = Path.cwd()

datas = [
    (str(ROOT / 'engine' / 'plantview-dwg.exe'), 'engine'),
    (str(ROOT / 'examples'), 'examples'),
    (str(ROOT / 'resources'), 'resources'),
    (str(ROOT / 'licenses'), 'licenses'),
    (str(ROOT / 'LICENSE'), '.'),
    (str(ROOT / 'THIRD_PARTY.md'), '.'),
]

hiddenimports = [
    'pyvistaqt',
    'pyvista',
    'vtk',
    'vtkmodules',
    'vtkmodules.all',
    'vtkmodules.vtkRenderingOpenGL2',
    'vtkmodules.vtkInteractionStyle',
    'vtkmodules.vtkRenderingFreeType',
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'ifcopenshell',
    'ezdxf',
    'unittest',
]

a = Analysis(
    ['run.py'],
    pathex=[str(ROOT)],
    binaries=collect_dynamic_libs('ifcopenshell'),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Qt uses Windows ICU. The build host PATH also contains Poppler ICU, whose
# versioned exports are incompatible with Qt's Windows ICU imports.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]

# Some bundled CAD dependency licenses have >260-character nested source paths.
# Keep every license verbatim under short stable names, with an index to the source path.
import hashlib
import json
license_index = {}
short_datas = []
for dest, source, kind in a.datas:
    if dest.replace('\\','/').startswith('licenses/') and len(dest) > 95:
        short = 'licenses/long-path/' + hashlib.sha256(dest.encode()).hexdigest()[:20] + Path(dest).suffix
        license_index[short] = dest
        dest = short
    short_datas.append((dest, source, kind))
index_file = ROOT / 'build' / 'license-path-index.json'
index_file.parent.mkdir(exist_ok=True)
index_file.write_text(json.dumps(license_index, indent=2), encoding='utf-8')
a.datas = short_datas + [('licenses/license-path-index.json', str(index_file), 'DATA')]
a.datas = [entry for entry in a.datas if not entry[0].replace('\\', '/').startswith('resources/web/host-build/')]

pyz = PYZ(a.pure, [], cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NavModel Manager',
    version=str(ROOT / 'resources' / 'version_info.txt'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / 'resources' / 'navmodel.ico'),
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='NavModel Manager')
