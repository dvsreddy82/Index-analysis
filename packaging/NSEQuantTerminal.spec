# PyInstaller spec for the macOS app bundle.  Build with:  packaging/build_mac.sh
# Bundles the code, the chart page + TradingView Lightweight Charts, Plotly's JS and Qt WebEngine.
# Never bundles secrets: .env and ~/.nse_analytics stay outside; the app reads ~/.nse_analytics at run time.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = "1.0.0"

datas = [(str(ROOT / "chart_web"), "chart_web"), (str(ROOT / "vendor"), "vendor")]
datas += collect_data_files("plotly", includes=["package_data/**/*"])
datas += collect_data_files("certifi")

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=collect_submodules("PySide6.QtWebEngineCore") + ["AppKit", "Foundation", "objc"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
# ---- slim Qt: the WebEngine hook drags in all of Qt's QML modules (3D, Charts, Quick Controls…),
# which this widgets-only app never loads.  Drop them, then keep only the Qt frameworks the remaining
# binaries really link to (dependency closure via otool -L), so nothing needed can go missing.
import re
import subprocess


def _is_qml(dest):
    return "/Qt/qml/" in dest.replace("\\", "/") or dest.replace("\\", "/").startswith("PySide6/Qt/qml")


a.datas = [d for d in a.datas if not _is_qml(d[0])]
a.binaries = [b for b in a.binaries if not _is_qml(b[0])]
fw_re = re.compile(r"(Qt[A-Za-z0-9]+)\.framework")
needed, todo, seen = set(), [b[1] for b in a.binaries if not fw_re.search(b[0])], set()
fw_src = {}
for dest, src, kind in a.binaries:
    m = fw_re.search(dest)
    if m:
        fw_src.setdefault(m.group(1), []).append(src)
while todo:
    src = todo.pop()
    if src in seen:
        continue
    seen.add(src)
    try:
        out = subprocess.run(["otool", "-L", src], capture_output=True, text=True).stdout
    except OSError:
        continue
    for name in set(fw_re.findall(out)):
        if name not in needed:
            needed.add(name)
            todo += [x for x in fw_src.get(name, []) if Path(x).is_file()]
before = len({fw_re.search(b[0]).group(1) for b in a.binaries if fw_re.search(b[0])})
a.binaries = [b for b in a.binaries if not fw_re.search(b[0]) or fw_re.search(b[0]).group(1) in needed]
a.datas = [d for d in a.datas if not fw_re.search(d[0]) or fw_re.search(d[0]).group(1) in needed]
print(f"[slim-qt] Qt frameworks kept: {len(needed & set(fw_src))} of {before}: {sorted(needed & set(fw_src))}")

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NSE Quant Terminal",
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NSE Quant Terminal")
app = BUNDLE(
    coll,
    name="NSE Quant Terminal.app",
    icon=str(ROOT / "packaging" / "AppIcon.icns"),
    bundle_identifier="in.ravikiran.nsequantterminal",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "NSE Quant Terminal",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Read-only market analytics. Not investment advice.",
    },
)
