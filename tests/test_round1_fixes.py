"""Regression tests for the round 1 review of 1.8.0 (a local review note).

Each class pins one finding. Every one of these defects was invisible to the
existing suite because the mock supplies the return code: the bugs lived in
what the *remote* actually does — a pipeline's exit status, a byte count versus
a character count. Where that is the case the unit test pins the mechanism
(the script we send, the branch we take) and tests/test_integration.py pins the
behaviour against a real host.
"""

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import _format_result


# ---------------------------------------------------------------------------
# F3: stderr must survive a successful command
# ---------------------------------------------------------------------------
# Everything an HPC toolchain says while still exiting 0 was dropped: module
# load warnings, compiler diagnostics, srun allocation notes, conda solver
# messages. The model cannot react to what it never sees.

class TestStderrSurvivesSuccess:
    def test_warning_on_stderr_is_reported(self):
        result = _format_result(0, "real-output\n", "WARNING: module X not found\n")
        assert "real-output" in result
        assert "WARNING: module X not found" in result

    def test_stdout_still_leads(self):
        result = _format_result(0, "data\n", "noise\n")
        assert result.startswith("data")

    def test_stderr_only_is_not_no_output(self):
        result = _format_result(0, "", "only a warning\n")
        assert "only a warning" in result

    def test_clean_success_is_unchanged(self):
        assert _format_result(0, "data here\n", "") == "data here\n"

    def test_silent_success_is_still_no_output(self):
        assert _format_result(0, "", "") == "(no output)"

    def test_whitespace_stderr_is_not_reported(self):
        assert _format_result(0, "data\n", "   \n") == "data\n"


# ---------------------------------------------------------------------------
# F2: a missing file must not read as an empty one
# ---------------------------------------------------------------------------
# `head -n N -- path | head -c probe` takes its exit status from the last
# command in the pipeline, and `head -c` succeeds on empty input. A failed
# `head -n` therefore returned 0, and with F3 unfixed its stderr was dropped
# too, so a missing file and an empty file were indistinguishable.

class TestMaxLinesDoesNotMaskFailure:
    def test_the_file_is_probed_before_the_pipeline(self, mock_subprocess):
        """The pipeline's status belongs to `head -c`, so the file has to be
        opened once on its own for a missing path to surface at all."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="l1\n")
        ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/x.log", max_lines=5)
        script = mock_subprocess.call_args.kwargs.get("input")
        first = script.splitlines()[0]
        assert first.startswith("head -c 1 -- "), script
        assert "|| exit" in first, script

    def test_pipefail_is_not_used(self, mock_subprocess):
        """pipefail looked right and was wrong: `head -c` closes the pipe as
        soon as it has its bytes, `head -n` dies of SIGPIPE, and a correctly
        truncated read came back as [EXIT CODE 141]."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="l1\n")
        ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/x.log", max_lines=5)
        assert "pipefail" not in mock_subprocess.call_args.kwargs.get("input")

    def test_a_whole_file_read_still_sends_one_command(self, mock_subprocess):
        """No line cap, no pipeline: keep the script as small as it was."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="l1\n")
        ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/x.log")
        script = mock_subprocess.call_args.kwargs.get("input")
        assert "|" not in script, script
        assert script.count("head") == 1, script


# ---------------------------------------------------------------------------
# F1: the byte cap must be counted in bytes
# ---------------------------------------------------------------------------
# The remote is asked for `head -c {max_bytes + 1}` -- a byte count -- and the
# reply was tested with `len(out) > max_bytes`, a *character* count. For any
# non-ASCII file the character count is smaller, so the truncation notice never
# fired: a 300 KB UTF-8 log came back as 150 KB with no indication, and with a
# U+FFFD where head -c had cut a codepoint in half.

REPLACEMENT = "�"


class TestByteCapIsCountedInBytes:
    def test_multibyte_file_over_the_cap_is_flagged(self, mock_subprocess):
        # 6 x 'é' is 6 characters but 12 bytes; the cap is 10.
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="é" * 6)
        result = ssh_hpc_server.read_remote_file(
            host="derecho", remote_path="/tmp/log", max_bytes=10,
        )
        assert "truncated at 10 bytes" in result, result

    def test_what_comes_back_honours_the_byte_cap(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="é" * 6)
        result = ssh_hpc_server.read_remote_file(
            host="derecho", remote_path="/tmp/log", max_bytes=10,
        )
        body = result.split("\n[truncated")[0]
        assert len(body.encode("utf-8")) <= 10, len(body.encode("utf-8"))

    def test_a_codepoint_split_by_head_is_dropped_not_returned_mangled(self, mock_subprocess):
        """head -c cuts on a byte boundary; the partial character must not survive."""
        wire = ("é" * 6).encode("utf-8")[:11].decode("utf-8", "replace")
        assert wire.endswith(REPLACEMENT)  # what _run_raw really hands us
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=wire)
        result = ssh_hpc_server.read_remote_file(
            host="derecho", remote_path="/tmp/log", max_bytes=10,
        )
        assert REPLACEMENT not in result, result

    def test_the_binary_notice_counts_bytes_too(self, mock_subprocess):
        """Same mislabel: the message said "bytes" and printed a character count."""
        # 3 x 'é' plus a NUL is 4 characters but 7 bytes.
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="é" * 3 + "\x00",
        )
        result = ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/bin")
        assert "binary" in result.lower()
        assert "first 7 bytes" in result, result

    def test_ascii_over_the_cap_is_still_flagged(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="a" * 11)
        result = ssh_hpc_server.read_remote_file(
            host="derecho", remote_path="/tmp/log", max_bytes=10,
        )
        assert "truncated at 10 bytes" in result, result

    @pytest.mark.parametrize("content", ["a" * 10, "é" * 5])
    def test_a_file_that_exactly_fills_the_cap_is_not_flagged(self, mock_subprocess, content):
        assert len(content.encode("utf-8")) == 10
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=content)
        result = ssh_hpc_server.read_remote_file(
            host="derecho", remote_path="/tmp/log", max_bytes=10,
        )
        assert "truncated" not in result, result
        assert result == content


# ---------------------------------------------------------------------------
# F5: the scheduler-poll limiter must not cache a failure
# ---------------------------------------------------------------------------
# _cached_poll stored whatever the producer returned, including an ssh exit-255
# failure and its "run ssh -fN <host>" hint. The user did exactly that, retried,
# and got the same stale error back -- relabelled "rate-limited, wait before
# polling again". A 30 s dead zone in the recovery loop this server exists for.

class TestOnlySuccessIsCached:
    def test_a_connection_failure_is_not_replayed(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="ssh: connect to host derecho port 22: Connection refused",
        )
        first = ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        second = ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        assert "[EXIT CODE 255]" in first
        assert "cached" not in second, second

    def test_a_failure_reaches_the_host_again(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=255, stderr="boom")
        ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        calls = mock_subprocess.call_count
        ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        assert mock_subprocess.call_count > calls

    def test_a_remote_command_failure_is_not_cached(self, mock_subprocess):
        """squeue missing is exit 127, not an ssh problem, but still not an answer."""
        mock_subprocess.return_value = make_completed_process(
            returncode=127, stderr="bash: line 1: squeue: command not found",
        )
        ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        second = ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        assert "cached" not in second, second

    def test_a_successful_answer_is_still_rate_limited(self, mock_subprocess):
        """The limiter must keep working: HPC centers flag agents that hammer squeue."""
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="JOBID PARTITION NAME\n123 amilan run1\n",
        )
        ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        calls = mock_subprocess.call_count
        second = ssh_hpc_server.list_queue(host="derecho", user="me", scheduler="slurm")
        assert "cached" in second, second
        assert mock_subprocess.call_count == calls

    def test_check_job_caches_only_success_too(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=255, stderr="boom")
        ssh_hpc_server.check_job(host="derecho", job_id="12345", scheduler="slurm")
        second = ssh_hpc_server.check_job(host="derecho", job_id="12345", scheduler="slurm")
        assert "cached" not in second, second


# ---------------------------------------------------------------------------
# F4: a partial annotation must not revert what it does not mention
# ---------------------------------------------------------------------------
# `entries[host] = pairs` replaced the host's entry outright. Replacing was
# deliberate and predates the JSON store (1.6.0 dropped every `# hpc-mcp:` line
# in the block and wrote one), and probe_host says so -- but `is_hpc: bool`
# cannot express "leave this alone", so every partial update asserted "this is
# an HPC system", and a recorded `hpc=false` machine silently became a
# policy-strict login node again.

@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "hosts.json"
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(tmp_path / "no-such-ssh-config"))
    ssh_hpc_server._DIRECTIVE_CACHE = None
    yield path
    ssh_hpc_server._DIRECTIVE_CACHE = None


GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"


class TestPartialAnnotationKeepsTheRest:
    def test_hpc_false_survives_a_later_unrelated_annotation(self, store):
        """The model records an account and never mentions is_hpc; the laptop
        must not turn back into an HPC login node."""
        ssh_hpc_server.annotate_host("venus", is_hpc=False)
        assert ssh_hpc_server._policy_mode("venus") == "off"
        ssh_hpc_server.annotate_host("venus", account="UABC0001")
        assert ssh_hpc_server._is_hpc("venus") is False
        assert ssh_hpc_server._policy_mode("venus") == "off"

    def test_a_key_that_was_not_passed_is_kept(self, store):
        ssh_hpc_server.annotate_host(
            "derecho", center="ncar", role="login", account="UABC0001",
            scratch="/glade/derecho/scratch/x", globus=GLADE,
        )
        ssh_hpc_server.annotate_host("derecho", policy="permissive")
        directives = ssh_hpc_server._host_directives("derecho")
        assert directives["center"] == "ncar"
        assert directives["account"] == "UABC0001"
        assert directives["globus"] == GLADE
        assert directives["policy"] == "permissive"

    def test_a_recorded_globus_uuid_is_not_lost(self, store):
        """The 1.8.0 changelog treats a write-only UUID as a bug; so is a
        UUID that the next annotation deletes."""
        ssh_hpc_server.annotate_host("derecho", center="ncar", globus=GLADE)
        ssh_hpc_server.annotate_host("derecho", account="UABC0001")
        assert ssh_hpc_server._resolve_collection("derecho") == GLADE

    def test_a_repassed_key_is_still_overwritten(self, store):
        ssh_hpc_server.annotate_host("derecho", center="ncar", account="OLD001")
        ssh_hpc_server.annotate_host("derecho", account="NEW002")
        assert ssh_hpc_server._host_directives("derecho")["account"] == "NEW002"
        assert "OLD001" not in store.read_text()

    def test_the_result_reports_everything_now_recorded(self, store):
        """Reporting only the new pairs hid what the write had just changed."""
        ssh_hpc_server.annotate_host("derecho", center="ncar", account="UABC0001")
        result = ssh_hpc_server.annotate_host("derecho", policy="permissive")
        assert "center=ncar" in result, result
        assert "account=UABC0001" in result, result
        assert "policy=permissive" in result, result

    def test_other_hosts_are_untouched(self, store):
        ssh_hpc_server.annotate_host("alpine", center="curc", account="ucb999")
        ssh_hpc_server.annotate_host("derecho", center="ncar")
        assert ssh_hpc_server._host_directives("alpine")["account"] == "ucb999"
