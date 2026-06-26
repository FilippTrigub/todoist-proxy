"""Control CLI launcher tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def test_ui_command_execs_control_ui_with_port(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    log_file = tmp_path / "launcher.json"
    stub_python = tmp_path / "python-stub"
    stub_python.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"log_file = {str(log_file)!r}\n"
        "with open(log_file, 'w', encoding='utf-8') as handle:\n"
        "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, handle)\n"
    )
    stub_python.chmod(0o755)

    subprocess.run(
        [str(repo / "todoist-proxy"), "ui", "--port", "8765"],
        check=True,
        cwd=repo,
        env={**os.environ, "TODOIST_HERMES_PYTHON": str(stub_python)},
    )

    launched = json.loads(log_file.read_text(encoding="utf-8"))
    assert launched == {
        "argv": ["control_ui.py", "--port", "8765"],
        "cwd": str(repo),
    }
