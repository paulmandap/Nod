# PyInstaller build for Nod.
#
#     pip install -r requirements-build.txt
#     pyinstaller --clean --noconfirm nod.spec
#
# One-dir, not one-file. One-file re-extracts the whole ~400 MB payload into
# %TEMP% on every launch, which costs 10-30 seconds of startup and a duplicate
# copy of the app on disk. Self-extracting binaries are also flagged by
# antivirus far more often than a plain exe beside a folder, and when something
# does go wrong on a tester's machine, a directory you can look inside beats a
# temp folder that deletes itself on exit.
#
# datas is an explicit allowlist. Never ('.', '.') and never a glob over the
# project root: the API key lives in %USERPROFILE%\.nod, outside this tree, and
# keeping the list explicit is what guarantees a stray file never ships with it.
# datasets/tests/test_no_secrets.py checks the built artifact for that anyway,
# because an intention is not a guarantee.

from PyInstaller.utils.hooks import collect_data_files

datas = [
    ('copilot/speaker.ps1', 'copilot'),            # or Nod cannot speak
    ('datasets/commands.train.jsonl', 'datasets'),  # or local intent degrades
    ('README-FOR-TESTERS.md', '.'),
]

# soundcard reads a .h file from its own package directory at import time (CFFI
# in ABI mode). It ships a PyInstaller hook that already collects these, but
# listing them again costs nothing and survives a downgrade to a version that
# does not. Without them, audio fails with an error that looks nothing like a
# missing data file.
datas += collect_data_files('soundcard', includes=['*.h'])

# Piper phonemises through espeak-ng, which reads a 373-file data directory
# from inside its own package at runtime. Without it the voice loads and then
# produces silence, which is a maddening thing to debug from a screenshot.
datas += collect_data_files('piper', includes=['espeak-ng-data/**'])

# googleapiclient's discovery documents are handled after Analysis instead --
# see the filter below. Asking collect_data_files for one of them does not help,
# because PyInstaller's own googleapiclient hook collects all 586 regardless.

hiddenimports = [
    # Imported inside a function, so the analyser cannot see it. This is the
    # dependency that was missing from requirements.txt entirely and broke
    # every browser feature on every machine but the author's.
    'websocket',
    'pynput.keyboard._win32',
    'pynput.mouse._win32',
    'google.auth.transport.requests',
    'google.oauth2.credentials',
    'google_auth_oauthlib.flow',
    'googleapiclient.discovery',
    'dateutil.rrule',
    'piper',
    'piper.espeakbridge',
    'onnxruntime',
]

excludes = [
    # onnxruntime used to be excluded here, worth ~97 MB with sympy: nothing
    # needed it, because faster_whisper only imports it inside the Silero VAD
    # path and every transcribe() call passes vad_filter=False.
    #
    # Piper runs its voice model on onnxruntime, so it comes back. The
    # vad_filter assertion in test_startup.py stays anyway -- it now guards
    # against a silent 40 MB rather than a broken build, and the reasoning is
    # worth keeping written down either way.

    # Never imported by Nod.
    'tkinter', 'matplotlib', 'pandas', 'scipy', 'IPython', 'pytest',
    'setuptools', 'pip', 'wheel',

    # PyQt6 ships far more than the three modules the overlay uses.
    'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuick3D', 'PyQt6.QtQuickWidgets',
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebChannel',
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets', 'PyQt6.QtPdf',
    'PyQt6.QtPdfWidgets', 'PyQt6.QtBluetooth', 'PyQt6.QtNfc',
    'PyQt6.QtPositioning', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort',
    'PyQt6.QtCharts', 'PyQt6.QtDataVisualization', 'PyQt6.Qt3DCore',
    'PyQt6.QtNetworkAuth', 'PyQt6.QtSql', 'PyQt6.QtTest', 'PyQt6.QtDesigner',
    'PyQt6.QtHelp', 'PyQt6.QtOpenGL', 'PyQt6.QtOpenGLWidgets',
    'PyQt6.QtSpatialAudio', 'PyQt6.QtSvgWidgets', 'PyQt6.QtRemoteObjects',
    'PyQt6.QtTextToSpeech',
]

a = Analysis(
    ['nod_main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=excludes,
    noarchive=False,
)

# Drop 585 of the 586 Google API discovery documents.
#
# PyInstaller's bundled googleapiclient hook collects the whole
# discovery_cache/documents/ folder -- 100 MB, a fifth of the finished build --
# and it wins over anything the spec asks collect_data_files for. Filtering
# a.datas after Analysis is the one place that reliably overrides a hook.
#
# Nod calls exactly one API: build("calendar", "v3", ...) in copilot/agenda.py.
# Keeping the v3 document means the Calendar client still constructs offline;
# every other API in that folder is dead weight for a program that will never
# call it. If a future feature calls another Google API, add it here or it will
# fail only in the packaged build.
_KEEP_DISCOVERY = ("calendar.v3.json",)
_DISCOVERY = "googleapiclient/discovery_cache/documents/"

_before = len(a.datas)
a.datas = [
    entry for entry in a.datas
    if not (entry[0].replace("\\", "/").startswith(_DISCOVERY)
            and not entry[0].endswith(_KEEP_DISCOVERY))
]
print(f"nod.spec: dropped {_before - len(a.datas)} unused Google discovery "
      f"documents")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Nod',
    debug=False,
    strip=False,
    # UPX compression is one of the strongest antivirus signals on Windows, and
    # Nod already looks alarming to a heuristic scanner -- it installs a global
    # keyboard hook, records the microphone, captures the screen and drives a
    # browser over a debugging port. Every one of those is real, so the goal is
    # to avoid adding signals that are not.
    upx=False,
    console=False,          # windowed: see the stdio shim at the top of main.py
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Nod',
)
