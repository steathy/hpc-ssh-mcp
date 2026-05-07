# HPC SSH MCP Server — Hardening & Feature Expansion Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the existing 5-tool MCP server with a full test suite, then add 5 new tools (connection check, upload, queue listing, tail, cancel) — all test-driven.

**Architecture:** Single-file server (`ssh_hpc_server.py`) using `fastmcp` SDK. All SSH/SCP operations delegate to system binaries via `subprocess.run` in list form. Tests mock `subprocess.run` to avoid real SSH calls. New tools follow the same `_validate_host` → `shlex.quote` → `_run`/`_run_raw` pattern as existing tools.

**Tech Stack:** Python 3.10+, fastmcp 3.x, pytest, unittest.mock

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Project metadata, dependencies (add `pytest` to dev deps) |
| `ssh_hpc_server.py` | MCP server — all tools, helpers, entry point |
| `tests/__init__.py` | Package marker |
| `tests/conftest.py` | Shared fixtures (mock subprocess, mock SSH results) |
| `tests/test_helpers.py` | Tests for `_validate_host`, `_run_raw`, `_format_result`, `_run` |
| `tests/test_tools_existing.py` | Tests for the 5 existing MCP tools |
| `tests/test_tools_new.py` | Tests for the 5 new MCP tools |
| `.gitignore` | Standard Python gitignore |

---

### Task 1: Project Infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `.gitignore`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add dev dependencies to pyproject.toml**

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]
```

Append this after the `[build-system]` block in `pyproject.toml`.

- [ ] **Step 2: Create .gitignore**

```gitignore
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/
.pytest_cache/
```

- [ ] **Step 3: Create tests/__init__.py**

Empty file — just the package marker.

- [ ] **Step 4: Create tests/conftest.py with shared fixtures**

```python
from unittest.mock import patch, MagicMock
import subprocess

import pytest


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run and return the mock for configuration."""
    with patch("ssh_hpc_server.subprocess.run") as mock_run:
        yield mock_run


def make_completed_process(returncode=0, stdout="", stderr=""):
    """Helper to build a subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )
```

- [ ] **Step 5: Install dev dependencies**

Run: `uv pip install -e ".[dev]"`
Expected: pytest installs successfully.

- [ ] **Step 6: Initialize git repo**

```bash
git init
git add pyproject.toml ssh_hpc_server.py .gitignore tests/
git commit -m "feat: initial SSH HPC MCP server with project structure"
```

---

### Task 2: Test Internal Helpers

**Files:**
- Create: `tests/test_helpers.py`

- [ ] **Step 1: Write tests for _validate_host**

```python
import pytest

from ssh_hpc_server import _validate_host


class TestValidateHost:
    @pytest.mark.parametrize("host", [
        "derecho",
        "login.ncar.edu",
        "user@host",
        "my-cluster_01",
        "192.168.1.1",
    ])
    def test_accepts_valid_hosts(self, host):
        _validate_host(host)  # should not raise

    @pytest.mark.parametrize("host", [
        "",
        "host; rm -rf /",
        "host$(cmd)",
        "host | cat",
        "host\ninjection",
        "host`whoami`",
    ])
    def test_rejects_invalid_hosts(self, host):
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            _validate_host(host)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_helpers.py::TestValidateHost -v`
Expected: All pass (validation logic already exists).

- [ ] **Step 3: Write tests for _run_raw**

Add to `tests/test_helpers.py`:

```python
import subprocess
from unittest.mock import patch

from ssh_hpc_server import _run_raw


class TestRunRaw:
    def test_returns_success(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="hello\n", stderr="",
        )
        rc, out, err = _run_raw(["echo", "hello"])
        assert rc == 0
        assert out == "hello\n"
        assert err == ""

    def test_returns_nonzero_exit(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stdout="", stderr="not found\n",
        )
        rc, out, err = _run_raw(["false"])
        assert rc == 1
        assert err == "not found\n"

    def test_handles_timeout(self, mock_subprocess):
        mock_subprocess.side_effect = subprocess.TimeoutExpired(
            cmd=["ssh", "host", "sleep 999"], timeout=5,
        )
        rc, out, err = _run_raw(["ssh", "host", "sleep 999"], timeout=5)
        assert rc == -1
        assert "Timed out after 5s" in err

    def test_handles_missing_binary(self, mock_subprocess):
        mock_subprocess.side_effect = FileNotFoundError()
        rc, out, err = _run_raw(["nonexistent"])
        assert rc == -1
        assert "Command not found" in err

    def test_passes_input_data(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(returncode=0)
        _run_raw(["cat"], input_data="hello world")
        mock_subprocess.assert_called_once()
        call_kwargs = mock_subprocess.call_args
        assert call_kwargs.kwargs.get("input") == "hello world" or call_kwargs[1].get("input") == "hello world"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_helpers.py::TestRunRaw -v`
Expected: All pass.

- [ ] **Step 5: Write tests for _format_result**

Add to `tests/test_helpers.py`:

```python
from ssh_hpc_server import _format_result


class TestFormatResult:
    def test_success_with_output(self):
        result = _format_result(0, "data here\n", "")
        assert result == "data here\n"

    def test_success_no_output(self):
        result = _format_result(0, "", "")
        assert result == "(no output)"

    def test_success_whitespace_only(self):
        result = _format_result(0, "   \n", "")
        assert result == "(no output)"

    def test_failure_with_stderr(self):
        result = _format_result(1, "", "error msg\n")
        assert "[EXIT CODE 1]" in result
        assert "error msg" in result

    def test_failure_with_both(self):
        result = _format_result(2, "partial\n", "fatal\n")
        assert "[EXIT CODE 2]" in result
        assert "partial" in result
        assert "fatal" in result
```

- [ ] **Step 6: Run all helper tests**

Run: `uv run pytest tests/test_helpers.py -v`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add tests/test_helpers.py
git commit -m "test: add unit tests for internal helpers"
```

---

### Task 3: Test Existing MCP Tools

**Files:**
- Create: `tests/test_tools_existing.py`

- [ ] **Step 1: Write tests for execute_remote_bash**

```python
import subprocess
from unittest.mock import patch, call

import pytest

from tests.conftest import make_completed_process


class TestExecuteRemoteBash:
    def test_runs_ssh_command(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="file1.txt\nfile2.txt\n",
        )
        from ssh_hpc_server import execute_remote_bash
        result = execute_remote_bash(host="derecho", command="ls")
        assert "file1.txt" in result
        mock_subprocess.assert_called_once()
        cmd = mock_subprocess.call_args[0][0]
        assert cmd == ["ssh", "derecho", "ls"]

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import execute_remote_bash
        with pytest.raises(ValueError):
            execute_remote_bash(host="host; rm -rf /", command="ls")

    def test_returns_error_on_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=127, stderr="command not found\n",
        )
        from ssh_hpc_server import execute_remote_bash
        result = execute_remote_bash(host="derecho", command="nonexistent")
        assert "[EXIT CODE 127]" in result
        assert "command not found" in result

    def test_respects_timeout(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        from ssh_hpc_server import execute_remote_bash
        execute_remote_bash(host="derecho", command="ls", timeout=30)
        call_kwargs = mock_subprocess.call_args
        assert call_kwargs.kwargs.get("timeout") == 30 or call_kwargs[1].get("timeout") == 30
```

- [ ] **Step 2: Write tests for submit_slurm_job**

Append to `tests/test_tools_existing.py`:

```python
class TestSubmitSlurmJob:
    def test_writes_script_and_submits(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),  # cat > file
            make_completed_process(returncode=0, stdout="Submitted batch job 12345\n"),  # sbatch
        ]
        from ssh_hpc_server import submit_slurm_job
        result = submit_slurm_job(
            host="derecho",
            job_script_content="#!/bin/bash\n#SBATCH -N 1\necho hello",
        )
        assert "12345" in result
        assert mock_subprocess.call_count == 2
        # First call: write script via stdin pipe
        write_call = mock_subprocess.call_args_list[0]
        assert "cat >" in write_call[0][0][2]
        # Second call: sbatch
        submit_call = mock_subprocess.call_args_list[1]
        assert "sbatch" in submit_call[0][0][2]

    def test_reports_write_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Permission denied\n",
        )
        from ssh_hpc_server import submit_slurm_job
        result = submit_slurm_job(host="derecho", job_script_content="#!/bin/bash")
        assert "Failed to write script" in result
        assert mock_subprocess.call_count == 1  # should not attempt sbatch

    def test_pipes_content_via_stdin(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 99\n"),
        ]
        from ssh_hpc_server import submit_slurm_job
        script = "#!/bin/bash\necho test"
        submit_slurm_job(host="derecho", job_script_content=script)
        write_call_kwargs = mock_subprocess.call_args_list[0]
        assert write_call_kwargs.kwargs.get("input") == script or write_call_kwargs[1].get("input") == script

    def test_quotes_remote_filename(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 1\n"),
        ]
        from ssh_hpc_server import submit_slurm_job
        submit_slurm_job(
            host="derecho",
            job_script_content="#!/bin/bash",
            remote_filename="my script.sh",
        )
        write_cmd = mock_subprocess.call_args_list[0][0][0][2]
        # shlex.quote wraps in single quotes for filenames with spaces
        assert "'" in write_cmd
```

- [ ] **Step 3: Write tests for check_slurm_job**

Append to `tests/test_tools_existing.py`:

```python
class TestCheckSlurmJob:
    def test_queries_squeue_and_sacct(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="JOBID  PARTITION  NAME\n12345  main  test\n"),
            make_completed_process(returncode=0, stdout="JobID|JobName|State\n12345|test|COMPLETED\n"),
        ]
        from ssh_hpc_server import check_slurm_job
        result = check_slurm_job(host="derecho", job_id="12345")
        assert "squeue" in result
        assert "sacct" in result
        assert "12345" in result
        assert mock_subprocess.call_count == 2

    def test_rejects_invalid_job_id(self):
        from ssh_hpc_server import check_slurm_job
        with pytest.raises(ValueError, match="Invalid Slurm job ID"):
            check_slurm_job(host="derecho", job_id="12345; rm -rf /")

    def test_accepts_array_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import check_slurm_job
        result = check_slurm_job(host="derecho", job_id="12345_0")
        assert mock_subprocess.call_count == 2  # squeue + sacct both called

    def test_accepts_step_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import check_slurm_job
        check_slurm_job(host="derecho", job_id="12345.0")
        assert mock_subprocess.call_count == 2
```

- [ ] **Step 4: Write tests for read_remote_file**

Append to `tests/test_tools_existing.py`:

```python
class TestReadRemoteFile:
    def test_reads_file_via_cat(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\nline2\n",
        )
        from ssh_hpc_server import read_remote_file
        result = read_remote_file(host="derecho", remote_path="/home/user/data.csv")
        assert "line1" in result
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "cat" in cmd[2]

    def test_uses_head_when_max_lines_set(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\n",
        )
        from ssh_hpc_server import read_remote_file
        read_remote_file(host="derecho", remote_path="/tmp/big.log", max_lines=100)
        cmd = mock_subprocess.call_args[0][0][2]
        assert "head -n 100" in cmd

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import read_remote_file
        read_remote_file(host="derecho", remote_path="/path with spaces/file.txt")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "'" in cmd  # shlex.quote wraps in single quotes
```

- [ ] **Step 5: Write tests for scp_download_file**

Append to `tests/test_tools_existing.py`:

```python
class TestScpDownloadFile:
    def test_calls_scp_correctly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import scp_download_file
        result = scp_download_file(
            host="derecho",
            remote_path="/data/output.nc",
            local_path="C:/Users/me/output.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        assert "derecho:" in cmd[1]
        assert cmd[2] == "C:/Users/me/output.nc"

    def test_reports_scp_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="No such file\n",
        )
        from ssh_hpc_server import scp_download_file
        result = scp_download_file(host="derecho", remote_path="/nope", local_path="/tmp/x")
        assert "[EXIT CODE 1]" in result
        assert "No such file" in result
```

- [ ] **Step 6: Run all existing tool tests**

Run: `uv run pytest tests/test_tools_existing.py -v`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add tests/test_tools_existing.py
git commit -m "test: add tests for all 5 existing MCP tools"
```

---

### Task 4: Add check_ssh_connection Tool

**Files:**
- Modify: `ssh_hpc_server.py`
- Create: `tests/test_tools_new.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools_new.py`:

```python
import subprocess
from unittest.mock import patch

import pytest

from tests.conftest import make_completed_process


class TestCheckSshConnection:
    def test_reports_healthy_socket(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="Master running (pid=12345)\n",
        )
        from ssh_hpc_server import check_ssh_connection
        result = check_ssh_connection(host="derecho")
        assert "running" in result.lower() or "Master" in result
        cmd = mock_subprocess.call_args[0][0]
        assert cmd == ["ssh", "-O", "check", "derecho"]

    def test_reports_dead_socket(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Control socket not found\n",
        )
        from ssh_hpc_server import check_ssh_connection
        result = check_ssh_connection(host="derecho")
        assert "[EXIT CODE 255]" in result

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import check_ssh_connection
        with pytest.raises(ValueError):
            check_ssh_connection(host="bad; host")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_new.py::TestCheckSshConnection -v`
Expected: FAIL with `ImportError: cannot import name 'check_ssh_connection'`

- [ ] **Step 3: Implement check_ssh_connection**

Add to `ssh_hpc_server.py` after the `scp_download_file` tool:

```python
@mcp.tool()
def check_ssh_connection(host: str) -> str:
    """Check if the SSH ControlMaster multiplex socket for a host is alive.

    Returns the socket status. Use this before running commands to verify
    the pre-authenticated session is still active.

    Args:
        host: SSH config alias or hostname.
    """
    _validate_host(host)
    return _run(["ssh", "-O", "check", host])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_new.py::TestCheckSshConnection -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add ssh_hpc_server.py tests/test_tools_new.py
git commit -m "feat: add check_ssh_connection tool"
```

---

### Task 5: Add scp_upload_file Tool

**Files:**
- Modify: `ssh_hpc_server.py`
- Modify: `tests/test_tools_new.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools_new.py`:

```python
class TestScpUploadFile:
    def test_calls_scp_upload_correctly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho",
            local_path="C:/Users/me/input.nc",
            remote_path="/scratch/user/input.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        assert cmd[1] == "C:/Users/me/input.nc"
        assert "derecho:" in cmd[2]

    def test_reports_upload_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Permission denied\n",
        )
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho", local_path="/tmp/x", remote_path="/nope",
        )
        assert "[EXIT CODE 1]" in result

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import scp_upload_file
        with pytest.raises(ValueError):
            scp_upload_file(host="bad; host", local_path="/a", remote_path="/b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_new.py::TestScpUploadFile -v`
Expected: FAIL with `ImportError: cannot import name 'scp_upload_file'`

- [ ] **Step 3: Implement scp_upload_file**

Add to `ssh_hpc_server.py` after `scp_download_file`:

```python
@mcp.tool()
def scp_upload_file(
    host: str,
    local_path: str,
    remote_path: str,
) -> str:
    """Upload a file from the local machine to a remote host via scp.

    Uses the system scp binary to respect SSH config and multiplex sockets.

    Args:
        host: SSH config alias or hostname.
        local_path: Path to the file on the local machine.
        remote_path: Destination path on the remote host.
    """
    _validate_host(host)
    escaped_remote = shlex.quote(remote_path)
    return _run(["scp", local_path, f"{host}:{escaped_remote}"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_new.py::TestScpUploadFile -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add ssh_hpc_server.py tests/test_tools_new.py
git commit -m "feat: add scp_upload_file tool"
```

---

### Task 6: Add list_slurm_queue Tool

**Files:**
- Modify: `ssh_hpc_server.py`
- Modify: `tests/test_tools_new.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools_new.py`:

```python
class TestListSlurmQueue:
    def test_queries_squeue(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0,
            stdout="JOBID  PARTITION  NAME  USER  ST  TIME  NODES\n12345  main  test  user1  R  1:00  1\n",
        )
        from ssh_hpc_server import list_slurm_queue
        result = list_slurm_queue(host="derecho")
        assert "12345" in result
        cmd = mock_subprocess.call_args[0][0][2]
        assert "squeue" in cmd

    def test_filters_by_user(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="header\n")
        from ssh_hpc_server import list_slurm_queue
        list_slurm_queue(host="derecho", user="jsmith")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "-u" in cmd
        assert "jsmith" in cmd

    def test_defaults_to_current_user(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="header\n")
        from ssh_hpc_server import list_slurm_queue
        list_slurm_queue(host="derecho")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "$USER" in cmd

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import list_slurm_queue
        with pytest.raises(ValueError):
            list_slurm_queue(host="bad; host")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_new.py::TestListSlurmQueue -v`
Expected: FAIL with `ImportError: cannot import name 'list_slurm_queue'`

- [ ] **Step 3: Implement list_slurm_queue**

Add to `ssh_hpc_server.py` after `check_slurm_job`:

```python
@mcp.tool()
def list_slurm_queue(host: str, user: str = "") -> str:
    """List Slurm jobs in the queue for a user.

    Defaults to the current user ($USER) if no user is specified.

    Args:
        host: SSH config alias for the HPC system.
        user: Username to filter by. Defaults to the remote $USER.
    """
    _validate_host(host)
    if user:
        safe_user = shlex.quote(user)
        cmd = f"squeue -u {safe_user} --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    else:
        cmd = "squeue -u $USER --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    return _run(["ssh", host, cmd])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_new.py::TestListSlurmQueue -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add ssh_hpc_server.py tests/test_tools_new.py
git commit -m "feat: add list_slurm_queue tool"
```

---

### Task 7: Add tail_remote_file Tool

**Files:**
- Modify: `ssh_hpc_server.py`
- Modify: `tests/test_tools_new.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools_new.py`:

```python
class TestTailRemoteFile:
    def test_tails_with_default_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="last line\n",
        )
        from ssh_hpc_server import tail_remote_file
        result = tail_remote_file(host="derecho", remote_path="/tmp/job.out")
        assert "last line" in result
        cmd = mock_subprocess.call_args[0][0][2]
        assert "tail -n 50" in cmd

    def test_tails_with_custom_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/tmp/x", lines=200)
        cmd = mock_subprocess.call_args[0][0][2]
        assert "tail -n 200" in cmd

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/path with spaces/out.log")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "'" in cmd  # shlex.quote wraps in single quotes

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import tail_remote_file
        with pytest.raises(ValueError):
            tail_remote_file(host="bad; host", remote_path="/tmp/x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_new.py::TestTailRemoteFile -v`
Expected: FAIL with `ImportError: cannot import name 'tail_remote_file'`

- [ ] **Step 3: Implement tail_remote_file**

Add to `ssh_hpc_server.py` after `read_remote_file`:

```python
@mcp.tool()
def tail_remote_file(
    host: str,
    remote_path: str,
    lines: int = 50,
) -> str:
    """Read the last N lines of a text file on a remote host.

    Ideal for checking the latest output from a running or completed Slurm job
    without reading the entire file.

    Args:
        host: SSH config alias or hostname.
        remote_path: Absolute or relative path to the file on the remote host.
        lines: Number of lines to read from the end (default 50).
    """
    _validate_host(host)
    safe_path = shlex.quote(remote_path)
    return _run(["ssh", host, f"tail -n {int(lines)} {safe_path}"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_new.py::TestTailRemoteFile -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add ssh_hpc_server.py tests/test_tools_new.py
git commit -m "feat: add tail_remote_file tool"
```

---

### Task 8: Add cancel_slurm_job Tool

**Files:**
- Modify: `ssh_hpc_server.py`
- Modify: `tests/test_tools_new.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools_new.py`:

```python
class TestCancelSlurmJob:
    def test_calls_scancel(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import cancel_slurm_job
        result = cancel_slurm_job(host="derecho", job_id="12345")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "scancel" in cmd[2]
        assert "'12345'" in cmd[2]

    def test_reports_cancel_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Invalid job id\n",
        )
        from ssh_hpc_server import cancel_slurm_job
        result = cancel_slurm_job(host="derecho", job_id="99999")
        assert "[EXIT CODE 1]" in result

    def test_rejects_invalid_job_id(self):
        from ssh_hpc_server import cancel_slurm_job
        with pytest.raises(ValueError, match="Invalid Slurm job ID"):
            cancel_slurm_job(host="derecho", job_id="12345; rm -rf /")

    def test_accepts_array_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import cancel_slurm_job
        cancel_slurm_job(host="derecho", job_id="12345_0")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "12345_0" in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tools_new.py::TestCancelSlurmJob -v`
Expected: FAIL with `ImportError: cannot import name 'cancel_slurm_job'`

- [ ] **Step 3: Implement cancel_slurm_job**

Add to `ssh_hpc_server.py` after `check_slurm_job`:

```python
@mcp.tool()
def cancel_slurm_job(host: str, job_id: str) -> str:
    """Cancel a Slurm job by its job ID.

    Args:
        host: SSH config alias for the HPC system.
        job_id: Slurm job ID to cancel (e.g. '12345', '12345_0' for array jobs).
    """
    _validate_host(host)
    if not _VALID_JOB_ID_RE.match(job_id):
        raise ValueError(
            f"Invalid Slurm job ID: {job_id!r}. "
            "Expected numeric ID, optionally with _ or . separators for array/step jobs."
        )
    safe_id = shlex.quote(job_id)
    return _run(["ssh", host, f"scancel {safe_id}"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_tools_new.py::TestCancelSlurmJob -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add ssh_hpc_server.py tests/test_tools_new.py
git commit -m "feat: add cancel_slurm_job tool"
```

---

### Task 9: Full Test Suite Run & Final Commit

**Files:**
- No new files — validation only.

- [ ] **Step 1: Run the complete test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests pass. Should be ~30+ tests across 3 files.

- [ ] **Step 2: Verify server imports cleanly**

Run: `uv run python -c "import ssh_hpc_server; print('Tools:', [t for t in dir(ssh_hpc_server) if not t.startswith('_') and callable(getattr(ssh_hpc_server, t, None))])"`
Expected: Lists all 10 tools (5 original + 5 new).

- [ ] **Step 3: Verify server starts on stdio**

Run: `echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' | timeout 5 uv run python ssh_hpc_server.py 2>/dev/null || true`
Expected: JSON-RPC response (or clean timeout — confirms server starts and listens on stdio).

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: full test suite passing, 10-tool MCP server complete"
```
