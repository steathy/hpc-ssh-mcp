"""Regression tests for the round 1 review of 1.10.0 (a local review note).

Each class pins one finding. As in the previous round, most of these were
invisible to the mocked suite: they lived in the store's caching, in a message
attached at the wrong layer, in text the server itself suggests, or in what a
build produces. Where the remote matters, tests/test_integration.py has the
live counterpart.
"""

import json
import subprocess

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server


# ---------------------------------------------------------------------------
# F3: a hand-edit of the settings file must be seen by the running server
# ---------------------------------------------------------------------------
# Every refusal tells the user to edit hosts.json and retry. The store was read
# once into a module cache that only record_host cleared, so the edit the
# refusal asked for had no effect until a restart the message never mentioned.

@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "hosts.json"
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    return path


class TestTheStoreIsReadNotCached:
    def test_a_policy_added_by_hand_applies_to_the_next_call(self, store):
        assert ssh_hpc_server._policy_mode("cluster") == "strict"      # primes any cache
        store.write_text(json.dumps({"hosts": {"cluster": {"policy": "off"}}}))
        assert ssh_hpc_server._policy_mode("cluster") == "off"

    def test_the_refusal_stops_once_the_user_has_done_what_it_said(self, store, mock_subprocess):
        first = ssh_hpc_server.execute_remote_bash(host="cluster", command="sudo ls")
        assert "Blocked by policy" in first
        store.write_text(json.dumps({"hosts": {"cluster": {"policy": "off"}}}))
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        assert "Blocked" not in ssh_hpc_server.execute_remote_bash(host="cluster", command="sudo ls")

    def test_a_globus_uuid_added_by_hand_resolves(self, store):
        uuid = "d33b3614-6d04-11e5-ba46-22000b92c6ec"
        with pytest.raises(ValueError):
            ssh_hpc_server._resolve_collection("derecho")
        store.write_text(json.dumps({"hosts": {"derecho": {"globus": uuid}}}))
        assert ssh_hpc_server._resolve_collection("derecho") == uuid

    def test_there_is_no_store_cache_to_go_stale(self):
        assert not hasattr(ssh_hpc_server, "_STORE_CACHE")


# ---------------------------------------------------------------------------
# F4: the scratch path the server suggests must be one submit_job can use
# ---------------------------------------------------------------------------
# probe_host proposed scratch=/glade/derecho/scratch/$USER and submit_job later
# said "pass remote_dir=<that>". But remote_dir goes through _shell_path, which is
# shlex.quote, so $USER was a literal directory name. Reproduced on a live host:
# a directory called "$USER" was created. The probe runs on the host, so it can
# simply report who the user is and the suggestion can be concrete.

def _probe_output(**over):
    fields = {
        "hostname": "derecho1.hpc.ucar.edu", "scheduler": "qsub", "account": "UABC0001",
        "filesystems": "/glade /glade/derecho/scratch", "globus": "yes", "user": "jdoe",
    }
    fields.update(over)
    return "\n".join(f"{k}={v}" for k, v in fields.items()) + "\n"


class TestSuggestedScratchIsConcrete:
    def test_the_probe_asks_the_host_who_the_user_is(self):
        assert "user=" in ssh_hpc_server._PROBE_SCRIPT
        assert '"$USER"' in ssh_hpc_server._PROBE_SCRIPT

    def test_ncar_scratch_names_the_user(self):
        guess = ssh_hpc_server._infer_from_probe(
            {"scheduler": "qsub", "filesystems": "/glade /glade/work", "user": "jdoe"})
        assert guess["scratch"] == "/glade/derecho/scratch/jdoe"

    def test_curc_scratch_names_the_user(self):
        guess = ssh_hpc_server._infer_from_probe(
            {"scheduler": "sbatch", "filesystems": "/scratch/alpine /pl/active", "user": "jdoe"})
        assert guess["scratch"] == "/scratch/alpine/jdoe"

    def test_no_user_means_no_guess_rather_than_a_placeholder(self):
        guess = ssh_hpc_server._infer_from_probe({"scheduler": "qsub", "filesystems": "/glade"})
        assert "scratch" not in guess

    def test_probe_host_output_carries_no_dollar_variable(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=_probe_output())
        result = ssh_hpc_server.probe_host("newbox")
        assert "$USER" not in result, result
        assert "scratch='/glade/derecho/scratch/jdoe'" in result, result

    def test_the_suggested_value_survives_shell_quoting_unchanged(self):
        """What submit_job will run: the quoted path must still be the path."""
        quoted = ssh_hpc_server._shell_path("/glade/derecho/scratch/jdoe")
        assert quoted.strip("'") == "/glade/derecho/scratch/jdoe"


# ---------------------------------------------------------------------------
# F6: the orphan-process note is true for a remote command and for nothing else
# ---------------------------------------------------------------------------
# It was attached in _run_raw, which every subprocess goes through. A timed-out
# scp does not leave a remote process (the sftp-server dies with the session:
# verified on a live host); the Globus CLI is a local process with no remote at
# all; and a timed-out run_on_compute leaves a *job*, which pgrep on the login
# node will never find.

def _timeout(cmd="ssh", seconds=3):
    return subprocess.TimeoutExpired(cmd=[cmd], timeout=seconds, output="", stderr="")


class TestTimeoutNoteIsAttachedWhereItIsTrue:
    def test_run_raw_itself_says_only_that_it_timed_out(self, mock_subprocess):
        mock_subprocess.side_effect = _timeout()
        rc, out, err = ssh_hpc_server._run_raw(["ssh", "h", "sleep 9"], timeout=3)
        assert rc == -1
        assert err.startswith("Timed out after 3s"), err
        assert "pgrep" not in err

    def test_a_remote_command_still_gets_the_warning(self, mock_subprocess):
        mock_subprocess.side_effect = _timeout()
        result = ssh_hpc_server.execute_remote_bash(host="derecho", command="sleep 300", timeout=3)
        assert "NOT stopped" in result and "pgrep" in result, result

    def test_a_raw_remote_command_gets_it_too(self, mock_subprocess):
        """read_remote_file and probe_host take the raw path; they must not lose it."""
        mock_subprocess.side_effect = _timeout()
        result = ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/x")
        assert "NOT stopped" in result, result

    def test_scp_does_not_claim_an_orphan(self, mock_subprocess, tmp_path):
        mock_subprocess.side_effect = _timeout("scp")
        result = ssh_hpc_server.scp_download_file("derecho", "/tmp/x.nc", str(tmp_path / "x.nc"), timeout=3)
        assert "Timed out after 3s" in result
        assert "pgrep" not in result and "NOT stopped" not in result, result

    def test_globus_does_not_claim_an_orphan(self, mock_subprocess, monkeypatch):
        monkeypatch.setattr(ssh_hpc_server.shutil, "which", lambda name: "/usr/bin/globus")
        mock_subprocess.side_effect = _timeout("globus", 120)
        result = ssh_hpc_server.globus_status()
        assert "Timed out" in result
        assert "pgrep" not in result and "remote command" not in result, result

    def test_run_on_compute_points_at_the_job_not_at_pgrep_alone(self, mock_subprocess):
        mock_subprocess.side_effect = _timeout()
        result = ssh_hpc_server.run_on_compute(
            host="derecho", command="python3 heavy.py", account="UABC0001", scheduler="pbs", timeout=3,
        )
        assert "list_queue" in result and "cancel_job" in result, result


# ---------------------------------------------------------------------------
# F5: read_remote_file returns the file, and decides "truncated" from a byte count
# ---------------------------------------------------------------------------
# The reply was decoded with errors="replace" and then re-encoded to measure it,
# so every undecodable byte became a 3-byte U+FFFD: a 300-byte Latin-1 log read in
# full was reported truncated and cut (live). The truncated branch also skipped
# the MAX_OUTPUT_CHARS cap, and the success branch went through the command
# formatter, which appended shell stderr to the file and turned a whitespace-only
# file into "(no output)". One return path now: the remote reports the byte count
# on stderr (`wc -c`, a stat), the body is what the remote sent, and the cap applies.

LATIN1_300 = "a" * 200 + "�" * 100   # what _run_raw hands us for 200 x 'a' + 100 x 0xE9


class TestReadRemoteFileReturnsTheFile:
    def _read(self, mock_subprocess, stdout, stderr, **kw):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=stdout, stderr=stderr)
        return ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/f", **kw)

    def test_the_remote_is_asked_for_the_byte_count(self, mock_subprocess):
        self._read(mock_subprocess, "x", "1\n")
        script = mock_subprocess.call_args.kwargs["input"]
        assert "wc -c" in script, script
        assert f"head -c {ssh_hpc_server.DEFAULT_MAX_BYTES} " in script, script

    def test_max_lines_counts_the_selected_lines(self, mock_subprocess):
        self._read(mock_subprocess, "x", "1\n", max_lines=5)
        script = mock_subprocess.call_args.kwargs["input"]
        assert "head -n 5 -- /tmp/f | wc -c" in script, script

    def test_a_latin1_file_read_in_full_is_not_called_truncated(self, mock_subprocess):
        result = self._read(mock_subprocess, LATIN1_300, "300\n", max_bytes=300)
        assert "truncated" not in result, result
        assert len(result) == 300, len(result)

    def test_a_latin1_file_that_is_longer_is_truncated_by_the_real_count(self, mock_subprocess):
        result = self._read(mock_subprocess, LATIN1_300, "500\n", max_bytes=300)
        assert "truncated at 300 of 500 bytes" in result, result
        assert result.startswith(LATIN1_300[:-1])

    def test_the_truncated_branch_honours_the_output_cap(self, mock_subprocess):
        result = self._read(mock_subprocess, "a" * 300_000, "400000\n", max_bytes=300_000)
        assert len(result) <= ssh_hpc_server.MAX_OUTPUT_CHARS + 200, len(result)
        assert "truncated to" in result, result[-200:]

    def test_shell_chatter_on_stderr_is_not_part_of_the_file(self, mock_subprocess):
        chatter = "Lmod Warning: module 'foo' not found\n12\n"
        assert self._read(mock_subprocess, "line1\nline2\n", chatter) == "line1\nline2\n"

    def test_a_whitespace_only_file_is_returned_as_is(self, mock_subprocess):
        assert self._read(mock_subprocess, "  \n\n", "4\n") == "  \n\n"

    def test_an_empty_file_still_says_so(self, mock_subprocess):
        assert self._read(mock_subprocess, "", "0\n") == "(no output)"

    def test_a_split_codepoint_at_the_cut_is_dropped(self, mock_subprocess):
        wire = ("é" * 6).encode("utf-8")[:11].decode("utf-8", "replace")
        result = self._read(mock_subprocess, wire, "12\n", max_bytes=11)
        assert "�" not in result, result
        assert result.startswith("é" * 5)

    def test_a_failure_is_still_reported_with_its_stderr(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stdout="", stderr="head: cannot open '/tmp/f' for reading: No such file or directory\n")
        result = ssh_hpc_server.read_remote_file(host="derecho", remote_path="/tmp/f")
        assert "[EXIT CODE 1]" in result and "No such file" in result


# ---------------------------------------------------------------------------
# F8: record_host can say "this is HPC after all"
# ---------------------------------------------------------------------------
# Round 1 of 1.8.0 (F4) made record_host merge rather than replace, so that a
# partial update could not silently revert hpc=false. The other direction was
# lost: is_hpc=True never wrote anything, so once a host was hpc=false no call
# could undo it, and the reply for record_host(is_hpc=True, ...) said hpc=False.
# is_hpc is a tri-state now: omitted leaves the key alone.

class TestHpcFalseCanBeUndone:
    def test_true_after_false_restores_the_policy(self, store):
        ssh_hpc_server.record_host("box", is_hpc=False)
        assert ssh_hpc_server._policy_mode("box") == "off"
        ssh_hpc_server.record_host("box", is_hpc=True, center="curc", role="login")
        assert ssh_hpc_server._is_hpc("box") is True
        assert ssh_hpc_server._policy_mode("box") == "strict"

    def test_the_reply_does_not_contradict_the_call(self, store):
        ssh_hpc_server.record_host("box", is_hpc=False)
        result = ssh_hpc_server.record_host("box", is_hpc=True, center="curc")
        assert "hpc=False" not in result, result

    def test_an_omitted_is_hpc_leaves_hpc_false_alone(self, store):
        """The F4 property, restated for the tri-state."""
        ssh_hpc_server.record_host("box", is_hpc=False)
        ssh_hpc_server.record_host("box", account="UABC0001")
        assert ssh_hpc_server._is_hpc("box") is False

    def test_true_on_a_fresh_host_is_nothing_to_write(self, store):
        result = ssh_hpc_server.record_host("box", is_hpc=True)
        assert "nothing" in result.lower(), result
        assert not store.exists()

    def test_true_alone_on_a_non_hpc_host_is_a_real_write(self, store):
        ssh_hpc_server.record_host("box", is_hpc=False)
        result = ssh_hpc_server.record_host("box", is_hpc=True)
        assert "nothing" not in result.lower(), result
        assert ssh_hpc_server._is_hpc("box") is True
        assert "hpc" not in json.loads(store.read_text())["hosts"]["box"]

    def test_the_default_is_omitted(self):
        import inspect
        assert inspect.signature(ssh_hpc_server.record_host).parameters["is_hpc"].default is None


# ---------------------------------------------------------------------------
# F10: scp results describe what actually happened to the file
# ---------------------------------------------------------------------------
# scp_upload_file computed the "[N GB transferred over scp ...]" notice before the
# transfer and appended it whatever the result, so a failed 3 GB upload was
# reported as transferred. scp_download_file removed a partial file only for a
# timeout into a new path; a dropped connection (rc 255) or an scp error (rc 1),
# or any failure while overwriting, left a truncated file with no mention.

@pytest.fixture
def sparse_3g(tmp_path):
    path = tmp_path / "sparse.bin"
    with open(path, "wb") as fh:
        fh.truncate(3_000_000_000)   # a sparse file: 3 GB by size, nothing on disk
    return path


class TestScpResultsMatchTheOutcome:
    def test_a_failed_upload_is_not_called_transferred(self, mock_subprocess, sparse_3g):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="kex_exchange_identification: Connection closed by remote host")
        result = ssh_hpc_server.scp_upload_file("derecho", str(sparse_3g), "/glade/x.bin")
        assert "[EXIT CODE 255]" in result
        assert "transferred over scp" not in result, result

    def test_a_successful_upload_still_gets_the_globus_pointer(self, mock_subprocess, sparse_3g):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        result = ssh_hpc_server.scp_upload_file("derecho", str(sparse_3g), "/glade/x.bin")
        assert "3.0 GB transferred over scp" in result, result

    def test_a_failed_download_into_a_new_path_is_removed(self, mock_subprocess, tmp_path):
        dest = tmp_path / "model.nc"
        def fail_leaving_a_partial(*args, **kwargs):
            dest.write_bytes(b"half of the fi")
            return make_completed_process(returncode=255, stderr="Connection closed by remote host")
        mock_subprocess.side_effect = fail_leaving_a_partial
        result = ssh_hpc_server.scp_download_file("derecho", "/data/model.nc", str(dest))
        assert not dest.exists(), "the partial file was left behind"
        assert "Partial download removed" in result, result

    def test_a_failed_download_over_an_existing_file_says_it_was_damaged(self, mock_subprocess, tmp_path):
        dest = tmp_path / "model.nc"
        dest.write_bytes(b"the original, all of it")
        def fail_after_overwriting(*args, **kwargs):
            dest.write_bytes(b"new but tru")
            return make_completed_process(returncode=1, stderr="scp: connection lost")
        mock_subprocess.side_effect = fail_after_overwriting
        result = ssh_hpc_server.scp_download_file("derecho", "/data/model.nc", str(dest))
        assert dest.exists()   # we cannot restore it, so we must not delete it
        assert "partially overwritten" in result, result

    def test_a_failure_that_left_an_existing_file_untouched_does_not_alarm(self, mock_subprocess, tmp_path):
        dest = tmp_path / "model.nc"
        dest.write_bytes(b"the original")
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="scp: /data/model.nc: No such file or directory")
        result = ssh_hpc_server.scp_download_file("derecho", "/data/model.nc", str(dest))
        assert dest.read_bytes() == b"the original"
        assert "overwritten" not in result, result

    def test_a_failure_that_wrote_nothing_says_nothing_about_the_file(self, mock_subprocess, tmp_path):
        dest = tmp_path / "model.nc"
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="scp: /data/model.nc: No such file or directory")
        result = ssh_hpc_server.scp_download_file("derecho", "/data/model.nc", str(dest))
        assert "Partial" not in result and "overwritten" not in result, result
