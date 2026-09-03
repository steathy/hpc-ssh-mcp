"""Live tests against a real SSH host. Skipped unless HPC_SSH_MCP_TEST_HOST is set.

    HPC_SSH_MCP_TEST_HOST=<ssh alias> uv run pytest tests/test_integration.py -v

The host needs non-interactive auth from this machine (a key or a live
ControlMaster socket), bash, and a writable /tmp. A scheduler is optional:
the detection test accepts either a scheduler or a clean "none found" error.
Everything created is removed again.

These exist because every other test mocks subprocess, and three of the
bugs fixed in 1.1.0 (SFTP-mode scp quoting, `ssh -O check` on stderr, the
Windows-style local path) were masked by mocks that encoded the wrong reality.
"""

import os
import uuid

import pytest

from ssh_hpc_server import (
    _detect_scheduler,
    check_ssh_connection,
    execute_remote_bash,
    read_remote_file,
    scp_download_file,
    scp_upload_file,
    tail_remote_file,
)

HOST = os.environ.get("HPC_SSH_MCP_TEST_HOST")
pytestmark = pytest.mark.skipif(
    not HOST, reason="set HPC_SSH_MCP_TEST_HOST=<ssh alias> to run live tests",
)


def _ok(result: str) -> str:
    assert "[EXIT CODE" not in result, result
    assert "Timed out" not in result, result
    return result


@pytest.fixture
def remote_dir():
    d = f"/tmp/hpc-ssh-mcp-test-{uuid.uuid4().hex[:8]}"
    _ok(execute_remote_bash(HOST, f"mkdir -p '{d}/sub dir'"))
    yield d
    execute_remote_bash(HOST, f"rm -rf '{d}'")


@pytest.fixture
def home_file():
    """A file under the remote $HOME, for '~' tests."""
    name = f".hpc-ssh-mcp-test-{uuid.uuid4().hex[:8]}"
    yield name
    execute_remote_bash(HOST, f"rm -f ~/{name}")


class TestLiveShell:
    def test_multiline_script_with_quotes_and_bang(self):
        out = _ok(execute_remote_bash(HOST, 'msg="don\'t stop!"\necho "$msg"\nprintf "a\\nb\\n" | wc -l\n'))
        assert "don't stop!" in out
        assert "2" in out

    def test_non_utf8_output_is_replaced_not_raised(self):
        out = _ok(execute_remote_bash(HOST, "printf 'caf\\xe9\\n'"))
        assert out.startswith("caf")

    def test_remote_stdin_is_eof_not_the_protocol_stream(self):
        out = _ok(execute_remote_bash(HOST, "cat; echo EOF-seen", timeout=20))
        assert "EOF-seen" in out

    def test_remote_failure_text_gets_no_reauth_hint(self):
        out = execute_remote_bash(HOST, "echo 'Permission denied (publickey).' >&2; exit 3")
        assert "[EXIT CODE 3]" in out
        assert "ssh -fN" not in out


class TestLiveFiles:
    def test_read_and_tail_paths_with_spaces_and_leading_dash(self, remote_dir):
        path = f"{remote_dir}/sub dir/-dash file.txt"
        _ok(execute_remote_bash(HOST, f"printf 'l1\\nl2\\nl3\\n' > '{path}'"))
        assert read_remote_file(HOST, path) == "l1\nl2\nl3\n"
        assert read_remote_file(HOST, path, max_lines=2) == "l1\nl2\n"
        assert tail_remote_file(HOST, path, lines=1) == "l3\n"

    def test_read_is_truncated_with_notice(self, remote_dir):
        path = f"{remote_dir}/big.txt"
        _ok(execute_remote_bash(HOST, f"head -c 5000 /dev/zero | tr '\\0' 'a' > '{path}'"))
        out = read_remote_file(HOST, path, max_bytes=100)
        assert out.startswith("a" * 100)
        assert "a" * 101 not in out
        assert "truncated" in out

    def test_binary_file_is_refused(self, remote_dir):
        path = f"{remote_dir}/bin.dat"
        _ok(execute_remote_bash(HOST, f"head -c 64 /dev/zero > '{path}'"))
        assert "binary" in read_remote_file(HOST, path).lower()

    def test_tilde_path_reaches_home(self, home_file):
        _ok(execute_remote_bash(HOST, f"echo tilde-ok > ~/{home_file}"))
        assert read_remote_file(HOST, f"~/{home_file}") == "tilde-ok\n"
        assert tail_remote_file(HOST, f"~/{home_file}") == "tilde-ok\n"


class TestLiveScp:
    def test_round_trip_with_spaces_remote_and_colon_local(self, remote_dir, tmp_path):
        payload = b"\x00\x01payload\xff"
        src = tmp_path / "my file.nc"
        src.write_bytes(payload)
        remote = f"{remote_dir}/sub dir/my file.nc"
        _ok(scp_upload_file(HOST, str(src), remote))
        back = tmp_path / "back:1.nc"
        _ok(scp_download_file(HOST, remote, str(back)))
        assert back.read_bytes() == payload

    def test_tilde_remote_path(self, home_file, tmp_path):
        src = tmp_path / "t.txt"
        src.write_text("via scp\n")
        _ok(scp_upload_file(HOST, str(src), f"~/{home_file}"))
        assert read_remote_file(HOST, f"~/{home_file}") == "via scp\n"
        back = tmp_path / "t-back.txt"
        _ok(scp_download_file(HOST, f"~/{home_file}", str(back)))
        assert back.read_text() == "via scp\n"


class TestLiveConnection:
    def test_check_ssh_connection_always_says_something(self):
        out = check_ssh_connection(HOST)
        assert out.strip()
        assert out != "(no output)"

    def test_scheduler_detection_names_one_or_says_none(self):
        try:
            assert _detect_scheduler(HOST) in ("pbs", "slurm")
        except ValueError as exc:
            assert "No PBS or Slurm" in str(exc)
