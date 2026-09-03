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

import ssh_hpc_server
from ssh_hpc_server import (
    _detect_scheduler,
    annotate_host,
    probe_host,
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
    # confirm_destructive: the policy layer refuses recursive deletes otherwise,
    # and a silently skipped teardown leaves directories on the host.
    _ok(execute_remote_bash(HOST, f"rm -rf '{d}'", confirm_destructive=True))


@pytest.fixture
def home_file():
    """A file under the remote $HOME, for '~' tests."""
    name = f".hpc-ssh-mcp-test-{uuid.uuid4().hex[:8]}"
    yield name
    _ok(execute_remote_bash(HOST, f"rm -f ~/{name}"))


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


class TestLivePolicy:
    """The guard runs before the connection, so these never reach the host."""

    def test_blocked_command_is_not_executed(self, remote_dir):
        marker = f"{remote_dir}/blocked-ran"
        out = execute_remote_bash(HOST, f"sudo touch '{marker}'")
        assert "Blocked" in out
        assert "No such file" in execute_remote_bash(HOST, f"ls '{marker}'")

    def test_confirm_tier_runs_only_with_the_flag(self, remote_dir):
        victim = f"{remote_dir}/sub dir"
        assert "confirm_destructive" in execute_remote_bash(HOST, f"rm -rf '{victim}'")
        assert _ok(execute_remote_bash(HOST, f"test -d '{victim}' && echo still-there")).strip() == "still-there"
        _ok(execute_remote_bash(HOST, f"rm -rf '{victim}'", confirm_destructive=True))
        assert "No such file" in execute_remote_bash(HOST, f"ls '{victim}'")

    def test_shared_root_traversal_never_reaches_the_host(self, remote_dir):
        """NSF NCAR: a traversal at a shared root is a metadata storm. Blocked locally."""
        marker = f"{remote_dir}/traversal-ran"
        out = execute_remote_bash(HOST, f"find /glade -name x > '{marker}'")
        assert "Blocked" in out
        assert "metadata" in out.lower()
        assert "No such file" in execute_remote_bash(HOST, f"ls '{marker}'")

    def test_traversal_inside_your_own_directory_is_allowed(self, remote_dir):
        out = _ok(execute_remote_bash(HOST, f"find '{remote_dir}' -maxdepth 1 -type d"))
        assert remote_dir in out

    def test_route_tier_runs_only_with_the_flag(self, remote_dir):
        script = f"{remote_dir}/hello.py"
        _ok(execute_remote_bash(HOST, f"printf 'print(1)\\n' > '{script}'"))
        out = execute_remote_bash(HOST, f"python3 '{script}'")
        assert "run_on_compute" in out
        assert _ok(execute_remote_bash(HOST, f"python3 '{script}'", allow_on_login_node=True)).strip() == "1"


class TestLiveOnboarding:
    """probe_host reads the real machine; annotate_host writes a scratch config."""

    @pytest.fixture
    def scratch_config(self, tmp_path, monkeypatch):
        """A throwaway ssh config and store. The real ~/.ssh/config is never
        touched by these tests, and must never be touched by the code either."""
        cfg = tmp_path / "config"
        cfg.write_text(f"Host {HOST}\n    HostName {HOST}\n    ControlMaster auto\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(tmp_path / "hosts.json"))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        ssh_hpc_server._ONBOARDING_SEEN.clear()
        yield cfg
        ssh_hpc_server._DIRECTIVE_CACHE = None
        ssh_hpc_server._ONBOARDING_SEEN.clear()

    def test_probe_reports_the_real_host(self, scratch_config):
        out = probe_host(HOST)
        assert "Detected:" in out
        assert "hostname" in out
        assert "annotate_host" in out or "is_hpc=False" in out

    def test_annotate_round_trip(self, scratch_config, tmp_path):
        import json
        before = scratch_config.read_bytes()
        assert "Recorded" in annotate_host(HOST, is_hpc=False)
        stored = json.loads((tmp_path / "hosts.json").read_text())
        assert stored["hosts"][HOST]["hpc"] is False
        assert scratch_config.read_bytes() == before  # ssh config untouched
        assert ssh_hpc_server._is_hpc(HOST) is False
        assert ssh_hpc_server._policy_mode(HOST) == "off"

    def test_non_hpc_host_runs_a_routed_command_without_a_flag(self, scratch_config):
        annotate_host(HOST, is_hpc=False)
        assert _ok(execute_remote_bash(HOST, "python3 -c 'print(41+1)'")).strip() == "42"


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
