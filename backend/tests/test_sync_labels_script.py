from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

LABELS_YML = """\
labels:
  - name: bug
    color: cc0000
    description: Something is broken
  - name: enhancement
    color: a2eeef
    description: New feature or request
"""

# UTF-8 text the fake gh CLI writes to stderr; deliberately not decodable as
# ASCII or as the Windows ANSI code pages, to exercise the decode path.
GH_STDERR_TEXT = "label 'bug' already exists — ünïcode ✓"


@pytest.fixture
def sync_checkout(tmp_path: Path) -> Path:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copyfile(REPO_ROOT / "scripts" / "sync_labels.py", scripts_dir / "sync_labels.py")
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    (github_dir / "labels.yml").write_text(LABELS_YML, encoding="utf-8")
    return tmp_path


def load_script_module(path: Path) -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_labels_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_labels_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_sync_survives_non_utf8_redirected_stdout(sync_checkout: Path, monkeypatch: pytest.MonkeyPatch):
    # Redirected stdout uses the ANSI code page on Windows and ASCII under the
    # POSIX C locale; neither can encode the success glyph the script prints.
    module = load_script_module(sync_checkout / "scripts" / "sync_labels.py")
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk" if os.name == "nt" else "ascii")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["sync_labels.py", "--repo", "octo/demo"])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=""),
    )

    assert module.main() == 0

    stream.flush()
    emitted = stream.buffer.getvalue()
    assert b"\xe2\x9c\x93" in emitted  # ✓ reached the log file as UTF-8
    assert "Done" in emitted.decode("utf-8")


def test_sync_label_decodes_gh_output_as_utf8(sync_checkout: Path, monkeypatch: pytest.MonkeyPatch):
    module = load_script_module(sync_checkout / "scripts" / "sync_labels.py")
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert module.sync_label({"name": "bug", "color": "cc0000"}, "octo/demo", dry_run=False) is True
    # gh writes UTF-8 regardless of host locale; the parent must not decode it
    # with the ANSI code page (cp1252/cp936), which mangles or crashes.
    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


@pytest.mark.skipif(os.name == "nt", reason="CreateProcess only resolves gh.exe; stub requires POSIX PATH search")
def test_sync_renders_utf8_gh_errors_without_crashing(sync_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    launcher = stub_dir / "gh"
    launcher.write_text(
        "#!/bin/sh\ncat >&2 <<'GHERR'\n" + GH_STDERR_TEXT + "\nGHERR\nexit 1\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    env = {**os.environ, "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", "")}
    # Force ASCII stdio in the child: without the fix it decodes gh's UTF-8
    # stderr with the C locale and dies with UnicodeDecodeError.
    env["PYTHONUTF8"] = "0"
    env["PYTHONCOERCECLOCALE"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env.pop("PYTHONIOENCODING", None)

    result = subprocess.run(
        [sys.executable, str(sync_checkout / "scripts" / "sync_labels.py"), "--repo", "octo/demo"],
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )

    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 1
    assert "✗" in stderr
    assert GH_STDERR_TEXT in stderr
    assert "Traceback" not in stderr
    assert "UnicodeDecodeError" not in stderr
    assert "label(s) failed to sync" in stderr
