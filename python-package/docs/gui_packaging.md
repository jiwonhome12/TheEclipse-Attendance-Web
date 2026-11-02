# GUI Packaging and Distribution

InsightFace Evaluation Studio supports development installs, PyPI package
installs, and desktop application builds.

## Development Install

```bash
cd python-package
pip install -e ".[gui]"
insightface-gui
```

## Build Python Package

```bash
cd python-package
python -m pip install build twine
python -m build
python -m twine check dist/*
```

## Optional face3d Extension

The default 1.0.1 package does not compile the optional `face3d` Cython/C++
extension. This avoids requiring a C++ compiler for normal inference and GUI
users.

To manually enable it, install the optional dependencies and pass the explicit
build flag:

```bash
cd python-package
pip install -e ".[face3d]" --no-build-isolation --config-settings editable_mode=compat
python setup.py build_ext --inplace --with-face3d
```

Equivalent environment-variable control:

```bash
INSIGHTFACE_WITH_FACE3D=1 python setup.py build_ext --inplace
```

The chosen parameter name is `--with-face3d`.

## Upload PyPI

The repository includes a guarded release helper for official PyPI. It builds
the source distribution and wheel, runs the GUI release smoke tests, checks that
the version is not already published, runs `twine check`, and asks for explicit
confirmation before upload.

Dry run:

```bash
cd python-package
python -m pip install build twine pytest
bash packaging/pypi/build_upload_pypi.sh --dry-run
```

Official upload:

```bash
cd python-package
export TWINE_USERNAME=__token__
export TWINE_PASSWORD='pypi-your-official-token'
bash packaging/pypi/build_upload_pypi.sh
```

The default release does not build `face3d`. To publish a package with the
optional extension enabled, run:

```bash
bash packaging/pypi/build_upload_pypi.sh --with-face3d
```

Manual equivalent:

```bash
cd python-package
python -m pip install build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Only project maintainers or CI configured with PyPI Trusted Publisher should
upload official PyPI releases. Codex should not attempt to upload PyPI.

Before releasing 1.0.1, confirm model licenses, README content, version numbers,
wheel contents, third-party notices, and that the package name is `insightface`.
PyPI versions are immutable, so a released version cannot be overwritten.

## Build Desktop Application

The first desktop packaging path uses PyInstaller one-folder mode.

Windows:

```powershell
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File packaging/desktop/build_windows.ps1
```

macOS:

```bash
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
bash packaging/desktop/build_macos.sh
```

Linux:

```bash
cd python-package
pip install -e ".[gui]"
pip install pyinstaller
bash packaging/desktop/build_linux.sh
```

Output:

- Windows: `.exe` or `dist/InsightFace Evaluation Studio/`
- macOS: `.app`, with `.dmg` creation left as a release step
- Linux: executable directory, with AppImage/deb creation left as a release step

## Notes

- The GUI extra uses `PySide6-Essentials` for Qt Widgets and includes
  `reportlab` for PDF reports without installing the much larger
  `PySide6_Addons` wheel.
- onnxruntime and onnxruntime-gpu may require additional dynamic library work.
- CUDA builds are not recommended for default community installers.
- A CPU provider build is the safest default.
- GPU/CUDA builds can be distributed as separate enterprise builds.
- Do not package user workspaces, SQLite databases, reports, images, videos, or
  embeddings.
- Do not package commercial model files by default.
- The GUI workspace/cache defaults to `~/.insightface/gui/`; model packages are
  manually downloaded into `~/.insightface/gui/cache/models` and extracted to
  `~/.insightface/models/<model_name>/`.
- Community builds must use PyInstaller one-folder mode, include third-party
  license notices, include LGPL license text, include Qt/PySide6 source offer,
  and must not restrict replacement of Qt/PySide6 shared libraries.
