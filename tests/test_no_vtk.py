"""maprt-client is the light half of the MapRT client: numpy and httpx2, never VTK.

It installs on Binder and Colab for the workshop, so importing it must not load VTK (the .obj surface
decode that needs VTK is the separate maprt-surface package), and it must not depend on another
workspace member, so it can be published on its own.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"  # audit: allow writable-locations - read only


def test_importing_every_module_loads_no_vtk():
    # A fresh interpreter: this test process has VTK loaded already by other suites.
    code = (
        "import sys, maprt_client, maprt_client.beams, maprt_client.http, maprt_client.recording, "
        "maprt_client.request, maprt_client.session_readme, maprt_client.wire\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'vtk', 'vtkmodules', 'vtk_arrays', 'rt_geometry', 'maprt_surface'})\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    command = [sys.executable, "-c", code]  # the running interpreter itself, not a PATH lookup
    result = subprocess.run(command, capture_output=True, text=True)  # audit: allow subprocesses - sys.executable
    assert result.returncode == 0, result.stdout + result.stderr


def test_depends_on_numpy_and_httpx2_only():
    dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    names = {d.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip() for d in dependencies}
    assert names == {"numpy", "httpx2"}
