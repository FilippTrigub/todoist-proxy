"""Control CLI launcher tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from control_ledger import ControlLedger


def _write_systemctl_stub(tmp_path: Path, output: str = "active") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {output!r}\n")
    systemctl.chmod(0o755)
    return bin_dir


def _write_recording_systemctl_stub(tmp_path: Path, log_file: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {str(log_file)!r}\n"
        "if [[ \"${1:-}\" == \"is-active\" ]]; then printf 'inactive\\n'; fi\n"
    )
    systemctl.chmod(0o755)
    return bin_dir


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


def test_status_prints_pending_queue_depth_without_real_systemd(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    control_home = tmp_path / "control"
    disable_file = tmp_path / "todoist-proxy.disabled"
    bin_dir = _write_systemctl_stub(tmp_path)
    ledger = ControlLedger(control_home=control_home)
    assert ledger.initialize_schema().success
    assert ledger.record_inbound_event_and_enqueue_pending(
        event_name="item:added",
        event_data={"id": "task-1", "project_id": "project-1"},
        raw_body=b'{"event_name":"item:added"}',
        headers={"X-Todoist-Delivery-ID": "delivery-1"},
        kind="delivery",
        subscription="inbox",
    ).success

    result = subprocess.run(
        [str(repo / "todoist-proxy"), "status"],
        check=True,
        cwd=repo,
        env={
            **os.environ,
            "CONTROL_HOME": str(control_home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TODOIST_DISABLE_FILE": str(disable_file),
        },
        text=True,
        capture_output=True,
    )

    assert "proxy:   ON  (forwarding active)" in result.stdout
    assert "service: active" in result.stdout
    assert f"file:    {disable_file}" in result.stdout
    assert "pending queue: 1" in result.stdout


def test_status_keeps_service_state_when_queue_depth_unavailable(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    control_home = tmp_path / "control"
    control_home.mkdir()
    (control_home / "todoist_interactions.db").mkdir()
    disable_file = tmp_path / "todoist-proxy.disabled"
    disable_file.touch()
    bin_dir = _write_systemctl_stub(tmp_path, "inactive")

    result = subprocess.run(
        [str(repo / "todoist-proxy"), "status"],
        check=True,
        cwd=repo,
        env={
            **os.environ,
            "CONTROL_HOME": str(control_home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TODOIST_DISABLE_FILE": str(disable_file),
        },
        text=True,
        capture_output=True,
    )

    assert "proxy:   OFF (disable file present)" in result.stdout
    assert "service: inactive" in result.stdout
    assert f"file:    {disable_file}" in result.stdout
    assert "pending queue: unavailable (database path is not a file:" in result.stdout


def test_spark_on_sets_gate_and_enables_timer_without_real_systemd(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    control_home = tmp_path / "control"
    log_file = tmp_path / "systemctl.log"
    bin_dir = _write_recording_systemctl_stub(tmp_path, log_file)

    result = subprocess.run(
        [str(repo / "todoist-proxy"), "spark", "on"],
        check=True,
        cwd=repo,
        env={
            **os.environ,
            "CONTROL_HOME": str(control_home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
    )
    config = json.loads((control_home / "todoist-control.json").read_text())

    assert config["global"]["spark_enabled"] is True
    assert "spark gate: ON" in result.stdout
    assert "spark timer: enabled and started" in result.stdout
    assert "enable --now report-cadence-poller.timer" in log_file.read_text()


def test_spark_off_sets_gate_and_disables_timer_without_real_systemd(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    control_home = tmp_path / "control"
    control_home.mkdir()
    (control_home / "todoist-control.json").write_text(
        json.dumps({"global": {"spark_enabled": True}}) + "\n"
    )
    log_file = tmp_path / "systemctl.log"
    bin_dir = _write_recording_systemctl_stub(tmp_path, log_file)

    result = subprocess.run(
        [str(repo / "todoist-proxy"), "spark", "off"],
        check=True,
        cwd=repo,
        env={
            **os.environ,
            "CONTROL_HOME": str(control_home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
    )
    config = json.loads((control_home / "todoist-control.json").read_text())

    assert config["global"]["spark_enabled"] is False
    assert "spark gate: OFF" in result.stdout
    assert "spark timer: disabled and stopped" in result.stdout
    assert "disable --now report-cadence-poller.timer" in log_file.read_text()


def test_spark_status_prints_gate_and_timer_without_real_systemd(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    control_home = tmp_path / "control"
    control_home.mkdir()
    (control_home / "todoist-control.json").write_text(
        json.dumps({"global": {"spark_enabled": False}}) + "\n"
    )
    bin_dir = _write_systemctl_stub(tmp_path, "inactive")

    result = subprocess.run(
        [str(repo / "todoist-proxy"), "spark", "status"],
        check=True,
        cwd=repo,
        env={
            **os.environ,
            "CONTROL_HOME": str(control_home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
    )

    assert "spark gate: OFF (disabled)" in result.stdout
    assert "timer:      inactive (report-cadence-poller.timer)" in result.stdout
