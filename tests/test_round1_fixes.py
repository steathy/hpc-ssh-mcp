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
    yield path


GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"


class TestPartialAnnotationKeepsTheRest:
    def test_hpc_false_survives_a_later_unrelated_annotation(self, store):
        """The model records an account and never mentions is_hpc; the laptop
        must not turn back into an HPC login node."""
        ssh_hpc_server.record_host("venus", is_hpc=False)
        assert ssh_hpc_server._policy_mode("venus") == "off"
        ssh_hpc_server.record_host("venus", account="UABC0001")
        assert ssh_hpc_server._is_hpc("venus") is False
        assert ssh_hpc_server._policy_mode("venus") == "off"

    def test_a_key_that_was_not_passed_is_kept(self, store):
        ssh_hpc_server.record_host(
            "derecho", center="ncar", role="login", account="UABC0001",
            scratch="/glade/derecho/scratch/x", globus=GLADE,
        )
        ssh_hpc_server.record_host("derecho", policy="permissive")
        directives = ssh_hpc_server._host_settings("derecho")
        assert directives["center"] == "ncar"
        assert directives["account"] == "UABC0001"
        assert directives["globus"] == GLADE
        assert directives["policy"] == "permissive"

    def test_a_recorded_globus_uuid_is_not_lost(self, store):
        """The 1.8.0 changelog treats a write-only UUID as a bug; so is a
        UUID that the next annotation deletes."""
        ssh_hpc_server.record_host("derecho", center="ncar", globus=GLADE)
        ssh_hpc_server.record_host("derecho", account="UABC0001")
        assert ssh_hpc_server._resolve_collection("derecho") == GLADE

    def test_a_repassed_key_is_still_overwritten(self, store):
        ssh_hpc_server.record_host("derecho", center="ncar", account="OLD001")
        ssh_hpc_server.record_host("derecho", account="NEW002")
        assert ssh_hpc_server._host_settings("derecho")["account"] == "NEW002"
        assert "OLD001" not in store.read_text()

    def test_the_result_reports_everything_now_recorded(self, store):
        """Reporting only the new pairs hid what the write had just changed."""
        ssh_hpc_server.record_host("derecho", center="ncar", account="UABC0001")
        result = ssh_hpc_server.record_host("derecho", policy="permissive")
        assert "center=ncar" in result, result
        assert "account=UABC0001" in result, result
        assert "policy=permissive" in result, result

    def test_other_hosts_are_untouched(self, store):
        ssh_hpc_server.record_host("alpine", center="curc", account="ucb999")
        ssh_hpc_server.record_host("derecho", center="ncar")
        assert ssh_hpc_server._host_settings("alpine")["account"] == "ucb999"


# ---------------------------------------------------------------------------
# F6: a quoted argument is one token
# ---------------------------------------------------------------------------
# _traversal_tier and _rm_tier split on whitespace, so a quoted *search
# pattern* was torn apart and its fragments read as flags and paths. Grepping
# your own notes for a dangerous command was blocked -- in the one tier with no
# override, which is the failure this project explicitly prefers to avoid.

class TestQuotedArgumentsAreOneToken:
    @pytest.mark.parametrize("command", [
        "grep -n 'rm -rf /' notes.md",
        'grep -r "temperature in /glade" mydir/',
        'grep -rn "du -sh /scratch" notes/',
    ])
    def test_a_quoted_pattern_is_not_read_as_a_path(self, command):
        tier, rule = ssh_hpc_server._classify_command(command, "login")
        assert tier == "free", f"{tier}: {rule}"

    @pytest.mark.parametrize("command", [
        "grep -r pattern /glade",
        "find /glade -name '*.nc'",
        "du -sh /scratch",
        "ls -R /",
        "rg needle /projects",
        "grep -r needle '/glade'",
    ])
    def test_real_traversals_still_block(self, command):
        assert ssh_hpc_server._classify_command(command, "login")[0] == "block", command

    def test_unbalanced_quotes_fall_back_instead_of_raising(self):
        tier, _ = ssh_hpc_server._classify_command("grep -r 'unclosed /glade", "login")
        assert tier in ("free", "block")  # must not raise

    def test_a_quoted_rm_target_is_still_seen(self):
        tier, _ = ssh_hpc_server._classify_command('rm -rf "$HOME"', "login")
        assert tier == "block"


# ---------------------------------------------------------------------------
# F7 (reverted): the traversal rule is login-node etiquette
# ---------------------------------------------------------------------------
# The review found the rule gated to login/dtn while the docs called it an
# absolute prohibition, and proposed either widening the rule or correcting the
# docs. Widening it was tried and reverted: `block` has no override in strict
# mode, so applying it on a compute node did not discourage a traversal, it made
# one unreachable through this server from anywhere. run_on_compute is the
# sanctioned route for heavy work and has to stay usable. The docs are what
# moved: this is login-node etiquette.

class TestTraversalIsLoginNodeEtiquette:
    @pytest.mark.parametrize("role", ["login", "dtn"])
    def test_shared_root_traversal_blocks_on_a_shared_entry_point(self, role):
        tier, _ = ssh_hpc_server._classify_command("find /glade -name '*.nc'", role)
        assert tier == "block", role

    def test_a_compute_node_is_the_users_own_call(self):
        """Deliberately routing it to a compute node is a considered choice, and
        the only path left once `block` refuses it everywhere else."""
        tier, _ = ssh_hpc_server._classify_command("find /glade -name '*.nc'", "compute")
        assert tier == "free"

    @pytest.mark.parametrize("role", ["login", "dtn", "compute"])
    def test_your_own_subdirectory_is_free_everywhere(self, role):
        tier, _ = ssh_hpc_server._classify_command("find /glade/work/me -name '*.nc'", role)
        assert tier == "free", role

    def test_run_on_compute_runs_it(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="4.0K\n")
        result = ssh_hpc_server.run_on_compute(
            host="derecho", command="du -sh /glade", account="UABC0001", scheduler="pbs",
        )
        assert "Blocked by policy" not in result
        mock_subprocess.assert_called_once()

    def test_execute_remote_bash_still_refuses_it(self, mock_subprocess):
        """The login node is where the etiquette applies, and there it holds."""
        result = ssh_hpc_server.execute_remote_bash(host="derecho", command="du -sh /glade")
        assert "Blocked by policy" in result
        mock_subprocess.assert_not_called()


# ---------------------------------------------------------------------------
# F16: the authorized_keys rule matched any filename containing the word
# ---------------------------------------------------------------------------

class TestAuthorizedKeysRuleIsAnchored:
    @pytest.mark.parametrize("command", [
        "echo hi > my_authorized_keys_notes.txt",
        "echo hi > notes-about-authorized_keys.md",
    ])
    def test_a_file_merely_named_after_it_is_free(self, command):
        assert ssh_hpc_server._classify_command(command, "login")[0] == "free", command

    @pytest.mark.parametrize("command", [
        "echo k >> ~/.ssh/authorized_keys",
        "echo k > /home/u/.ssh/authorized_keys",
        "tee -a /home/u/.ssh/authorized_keys",
        "echo k >>authorized_keys",
    ])
    def test_the_real_thing_still_blocks(self, command):
        assert ssh_hpc_server._classify_command(command, "login")[0] == "block", command


# ---------------------------------------------------------------------------
# F15: the output cap is a cap on what is returned
# ---------------------------------------------------------------------------
# stdout and stderr were each truncated to MAX_OUTPUT_CHARS and then joined,
# so a failing command could return twice the documented cap.

class TestOutputCapIsWhatItSays:
    def test_both_streams_together_stay_within_the_cap(self):
        big = "x" * (ssh_hpc_server.MAX_OUTPUT_CHARS * 2)
        result = _format_result(1, big, big)
        assert len(result) <= ssh_hpc_server.MAX_OUTPUT_CHARS + 200, len(result)

    def test_success_with_a_huge_stderr_is_capped_too(self):
        big = "x" * (ssh_hpc_server.MAX_OUTPUT_CHARS * 2)
        result = _format_result(0, big, big)
        assert len(result) <= ssh_hpc_server.MAX_OUTPUT_CHARS + 200, len(result)

    def test_it_says_it_truncated(self):
        big = "x" * (ssh_hpc_server.MAX_OUTPUT_CHARS * 2)
        assert "truncated" in _format_result(1, big, big)

    def test_small_output_is_untouched(self):
        assert _format_result(0, "hi\n", "") == "hi\n"
        assert "[EXIT CODE 1]" in _format_result(1, "out", "err")


# ---------------------------------------------------------------------------
# F8: a timed-out command is still running on the login node
# ---------------------------------------------------------------------------
# subprocess kills the *local* ssh client. Without a TTY the remote command
# keeps going, so "Timed out" invited a retry that stacked orphans on exactly
# the shared node this server's whole policy exists to protect.

class TestTimeoutSaysTheRemoteCommandSurvives:
    def test_the_message_warns_about_the_orphan(self, mock_subprocess):
        import subprocess as sp
        mock_subprocess.side_effect = sp.TimeoutExpired(cmd=["ssh"], timeout=3, output="start\n")
        result = ssh_hpc_server.execute_remote_bash(
            host="derecho", command="sleep 300", timeout=3,
        )
        assert "Timed out after 3s" in result
        lowered = result.lower()
        assert "not stopped" in lowered or "still running" in lowered, result
        assert "pgrep" in result  # and how to find it

    def test_partial_output_is_kept(self, mock_subprocess):
        import subprocess as sp
        mock_subprocess.side_effect = sp.TimeoutExpired(cmd=["ssh"], timeout=3, output="start\n")
        assert "start" in ssh_hpc_server.execute_remote_bash(
            host="derecho", command="sleep 300", timeout=3,
        )


# ---------------------------------------------------------------------------
# F14: a host with no ControlPath is not a broken host
# ---------------------------------------------------------------------------

class TestNoControlPathIsExplained:
    def test_the_verdict_is_interpreted(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr='No ControlPath specified for "-O" command\n',
        )
        result = ssh_hpc_server.check_ssh_connection(host="venus")
        assert "ControlPath" in result
        assert "multiplex" in result.lower()

    def test_a_live_master_still_reports_plainly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stderr="Master running (pid=1234)\n",
        )
        assert ssh_hpc_server.check_ssh_connection(host="derecho") == "Master running (pid=1234)"


# ---------------------------------------------------------------------------
# F17: housekeeping
# ---------------------------------------------------------------------------

class TestPollCacheDoesNotGrowForever:
    def test_expired_entries_are_dropped(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        ssh_hpc_server.list_queue(host="a", user="me", scheduler="slurm")
        ssh_hpc_server.list_queue(host="b", user="me", scheduler="slurm")
        assert len(ssh_hpc_server._POLL_CACHE) == 2
        # Age both entries past the window.
        stale = ssh_hpc_server.SCHEDULER_POLL_INTERVAL + 1
        ssh_hpc_server._POLL_CACHE = {
            k: (ts - stale, v) for k, (ts, v) in ssh_hpc_server._POLL_CACHE.items()
        }
        ssh_hpc_server.list_queue(host="c", user="me", scheduler="slurm")
        assert len(ssh_hpc_server._POLL_CACHE) == 1, ssh_hpc_server._POLL_CACHE

    def test_a_live_entry_is_not_dropped(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        ssh_hpc_server.list_queue(host="a", user="me", scheduler="slurm")
        ssh_hpc_server.list_queue(host="b", user="me", scheduler="slurm")
        assert "cached" in ssh_hpc_server.list_queue(host="a", user="me", scheduler="slurm")


class TestStoreWriteHasNoFixedTempName:
    def test_two_writers_cannot_collide_on_one_temp_path(self, tmp_path, monkeypatch):
        """FastMCP runs sync tools in a thread pool, so `f"{path}.tmp"` was a race."""
        import inspect
        source = inspect.getsource(ssh_hpc_server._write_store)
        assert 'f"{path}.tmp"' not in source, source
        assert "mkstemp" in source, source

    def test_the_store_is_still_written_privately_and_atomically(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "hosts.json"
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(target))
        assert ssh_hpc_server._write_store({"h": {"center": "ncar"}}) is None
        assert oct(target.stat().st_mode)[-3:] == "600"
        assert [p.name for p in target.parent.iterdir()] == ["hosts.json"]


class TestWalltimeAcceptsSlurmDayFormat:
    @pytest.mark.parametrize("walltime", ["00:30:00", "12:00:00", "240:00:00",
                                          "1-00:00:00", "2-12:30:00"])
    def test_accepted(self, walltime):
        ssh_hpc_server._validate_directive(
            "walltime", walltime, ssh_hpc_server._VALID_WALLTIME_RE,
        )

    @pytest.mark.parametrize("walltime", ["30:00", "1:2:3:4", "abc", "1-2", "; rm -rf /"])
    def test_rejected(self, walltime):
        with pytest.raises(ValueError, match="walltime"):
            ssh_hpc_server._validate_directive(
                "walltime", walltime, ssh_hpc_server._VALID_WALLTIME_RE,
            )


class TestDirectDependenciesAreDeclared:
    def test_mcp_is_declared_not_just_transitive(self):
        """`from mcp.types import ToolAnnotations` is a direct import."""
        import pathlib
        import tomllib
        root = pathlib.Path(ssh_hpc_server.__file__).parent
        meta = tomllib.loads((root / "pyproject.toml").read_text())
        # "fastmcp" contains "mcp": match the distribution name, not a substring.
        import re as _re
        names = {_re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0].strip().lower()
                 for d in meta["project"]["dependencies"]}
        assert "mcp" in names, names


# ---------------------------------------------------------------------------
# F9: submit_job truncates the remote file it writes
# ---------------------------------------------------------------------------
# The auto-generated claude_job_<hex>.sh really is additive, so the annotation
# stays -- but with an explicit remote_filename `cat >` replaces whatever is
# there, and the docstring never said so.

class TestSubmitJobSaysItOverwrites:
    def test_the_docstring_warns_about_an_existing_file(self):
        doc = ssh_hpc_server.submit_job.__doc__
        assert "overwrit" in doc.lower() or "replace" in doc.lower(), doc

    def test_the_annotation_is_still_additive(self):
        """The default filename is unique, so auto-approval stays reasonable."""
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        assert "@mcp.tool(annotations=_ADDITIVE)\ndef submit_job(" in source
