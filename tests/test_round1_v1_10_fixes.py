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


# ---------------------------------------------------------------------------
# F9: shared state is written under a lock, and SSH round trips are not
# ---------------------------------------------------------------------------
# FastMCP runs sync tools on worker threads and the MCP server handles requests
# concurrently. record_host is a read-modify-write of the store file, and two
# concurrent calls for different hosts lost one of them (reproduced). The poll
# cache is a dict that one thread iterated while another inserted. One lock,
# held for the store's read-merge-write and for the cache's bookkeeping, and
# deliberately *not* while a scheduler query is in flight: polls for different
# hosts must not queue behind each other's SSH round trips.

import threading


class TestSharedStateIsLocked:
    def test_two_concurrent_records_both_land(self, store, monkeypatch):
        import time
        real_write = ssh_hpc_server._write_store
        def slow_write(document):
            time.sleep(0.15)          # long enough for the other thread to have read
            return real_write(document)
        monkeypatch.setattr(ssh_hpc_server, "_write_store", slow_write)
        threads = [
            threading.Thread(target=ssh_hpc_server.record_host, args=("derecho",), kwargs={"center": "ncar"}),
            threading.Thread(target=ssh_hpc_server.record_host, args=("alpine",), kwargs={"center": "curc"}),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(json.loads(store.read_text())["hosts"]) == ["alpine", "derecho"]

    def test_the_store_is_written_under_the_lock(self, store, monkeypatch):
        seen = []
        real_write = ssh_hpc_server._write_store
        def observing_write(document):
            seen.append(ssh_hpc_server._STATE_LOCK.locked())
            return real_write(document)
        monkeypatch.setattr(ssh_hpc_server, "_write_store", observing_write)
        ssh_hpc_server.record_host("derecho", center="ncar")
        assert seen == [True]

    def test_a_scheduler_query_runs_outside_the_lock(self):
        held = []
        def produce():
            held.append(ssh_hpc_server._STATE_LOCK.locked())
            return True, "answer"
        ssh_hpc_server._POLL_CACHE.clear()
        assert ssh_hpc_server._cached_poll(("queue", "h", "q"), produce) == "answer"
        assert held == [False]

    def test_the_cache_still_replays_and_expires(self, monkeypatch):
        ssh_hpc_server._POLL_CACHE.clear()
        calls = []
        def produce():
            calls.append(1)
            return True, "answer"
        first = ssh_hpc_server._cached_poll(("queue", "h", "q"), produce)
        second = ssh_hpc_server._cached_poll(("queue", "h", "q"), produce)
        assert first == "answer" and second.startswith("answer\n[cached")
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# F11: a generic mount alone does not make a host HPC
# ---------------------------------------------------------------------------
# /projects exists on plenty of ordinary Linux machines. With it and no
# scheduler the probe declared the host HPC, inferred no centre and no role, and
# printed an empty "Suggested settings" followed by `record_host(host='box', )`.

class TestProbeNeedsMoreThanAGenericMount:
    def test_a_bare_projects_mount_is_not_hpc(self):
        guess = ssh_hpc_server._infer_from_probe({"scheduler": "", "filesystems": "/projects", "hostname": "box"})
        assert guess["is_hpc"] is False

    def test_a_centre_mount_is(self):
        guess = ssh_hpc_server._infer_from_probe({"scheduler": "", "filesystems": "/scratch/alpine /projects"})
        assert guess["is_hpc"] is True and guess["center"] == "curc"

    def test_a_scheduler_is(self):
        assert ssh_hpc_server._infer_from_probe({"scheduler": "qsub", "filesystems": ""})["is_hpc"] is True

    def test_the_probe_asks_rather_than_proposing_nothing(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout=_probe_output(scheduler="", filesystems="/projects", account="", globus="no"))
        result = ssh_hpc_server.probe_host("box")
        assert "record_host(host='box', )" not in result, result
        assert "does not look like a shared HPC system" in result, result


# ---------------------------------------------------------------------------
# F14: _validate_setting left the 1.7.0 comment format behind in 1.8.0
# ---------------------------------------------------------------------------
# Its docstring still said values "live in a single-line, space-separated comment",
# and it rejected whitespace and '#' for that reason. The store has been JSON since
# 1.8.0; a path with a space or a '#' is a legal path. record_host was also
# case-sensitive where every reader lowercases, and is_hpc=False dropped a globus
# UUID, which is not an HPC-only setting.

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"


class TestSettingsValidationMatchesTheJsonStore:
    @pytest.mark.parametrize("value", ["/glade/work/u/run#1", "/glade/work/u/my run"])
    def test_a_legal_path_is_accepted(self, store, value):
        ssh_hpc_server.record_host("derecho", scratch=value)
        assert ssh_hpc_server._host_settings("derecho")["scratch"] == value

    def test_a_control_character_is_still_refused(self, store):
        with pytest.raises(ValueError, match="scratch"):
            ssh_hpc_server.record_host("derecho", scratch="/x\npolicy=off")

    def test_the_docstring_no_longer_describes_a_comment(self):
        assert "comment" not in (ssh_hpc_server._validate_setting.__doc__ or "").lower()

    @pytest.mark.parametrize("kwargs,key,stored", [
        ({"role": "Login"}, "role", "login"),
        ({"role": "Data-Access"}, "role", "dtn"),
        ({"center": "NCAR"}, "center", "ncar"),
        ({"policy": "OFF"}, "policy", "off"),
    ])
    def test_case_is_folded_as_the_readers_fold_it(self, store, kwargs, key, stored):
        ssh_hpc_server.record_host("derecho", **kwargs)
        assert ssh_hpc_server._host_settings("derecho")[key] == stored

    def test_a_non_hpc_host_keeps_its_globus_collection(self, store):
        ssh_hpc_server.record_host("laptop", is_hpc=False, globus=GLADE)
        assert ssh_hpc_server._is_hpc("laptop") is False
        assert ssh_hpc_server._resolve_collection("laptop") == GLADE


# ---------------------------------------------------------------------------
# F15: record_host rewrites the file it read, not a filtered copy of it
# ---------------------------------------------------------------------------
# The reader kept only hosts matching the alias pattern and only scalar values,
# and record_host wrote that filtered dict back, so hand-written content the
# readers could not use was dropped without a word -- from a file whose own note
# says "Safe to edit". The readers still filter; the writer merges into the
# document as it was.

HAND_WRITTEN = {
    "_comment": "mine, please keep",
    "hosts": {
        "derecho": {"center": "ncar", "scratch": ["/a", "/b"], "note": "a list the server ignores"},
        "odd:alias": {"center": "curc"},
        "broken": "not an object",
    },
}


class TestRecordHostPreservesWhatItDoesNotUnderstand:
    def _stored(self, store):
        return json.loads(store.read_text())

    def test_other_hosts_keep_their_hand_written_values(self, store):
        store.write_text(json.dumps(HAND_WRITTEN))
        ssh_hpc_server.record_host("casper", account="UABC0001")
        hosts = self._stored(store)["hosts"]
        assert hosts["derecho"]["scratch"] == ["/a", "/b"]
        assert hosts["odd:alias"] == {"center": "curc"}
        assert hosts["broken"] == "not an object"
        assert hosts["casper"] == {"account": "UABC0001"}

    def test_top_level_keys_survive(self, store):
        store.write_text(json.dumps(HAND_WRITTEN))
        ssh_hpc_server.record_host("casper", account="UABC0001")
        doc = self._stored(store)
        assert doc["_comment"] == "mine, please keep"
        assert "_note" in doc   # ours, still written

    def test_the_recorded_host_keeps_its_own_unknown_values(self, store):
        store.write_text(json.dumps(HAND_WRITTEN))
        ssh_hpc_server.record_host("derecho", account="UABC0001")
        entry = self._stored(store)["hosts"]["derecho"]
        assert entry["scratch"] == ["/a", "/b"] and entry["account"] == "UABC0001"

    def test_a_host_that_was_not_an_object_becomes_one(self, store):
        store.write_text(json.dumps(HAND_WRITTEN))
        ssh_hpc_server.record_host("broken", center="ncar")
        assert self._stored(store)["hosts"]["broken"] == {"center": "ncar"}

    def test_readers_still_ignore_what_they_cannot_use(self, store):
        store.write_text(json.dumps(HAND_WRITTEN))
        assert ssh_hpc_server._host_settings("derecho") == {"center": "ncar", "note": "a list the server ignores"}
        assert ssh_hpc_server._host_settings("odd:alias") == {}


# ---------------------------------------------------------------------------
# F16: the first-use notice belongs to command tools, once
# ---------------------------------------------------------------------------
# It was appended inside _run_ssh_checked, so the poll cache stored it and
# replayed the "say this once" notice for thirty seconds, and tail_remote_file
# returned it glued to the file's last lines while read_remote_file never did.
# Command tools carry it; file tools and the cache do not.

class TestOnboardingNoticePlacement:
    def test_a_cached_poll_does_not_replay_it(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="JOBID PARTITION\n")
        first = ssh_hpc_server.list_queue("newbox", scheduler="slurm")
        second = ssh_hpc_server.list_queue("newbox", scheduler="slurm")
        assert "[first use" in first
        assert "[cached" in second and "[first use" not in second, second

    def test_tail_returns_only_the_file(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="last line\n")
        assert ssh_hpc_server.tail_remote_file("newbox", "/tmp/x.log") == "last line\n"

    def test_read_returns_only_the_file(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="content\n", stderr="8\n")
        assert ssh_hpc_server.read_remote_file("newbox", "/tmp/x.log") == "content\n"

    @pytest.mark.parametrize("call", [
        lambda: ssh_hpc_server.execute_remote_bash("newbox", "echo hi"),
        lambda: ssh_hpc_server.check_job("newbox", "123", scheduler="slurm"),
        lambda: ssh_hpc_server.cancel_job("newbox", "123", scheduler="slurm"),
        lambda: ssh_hpc_server.run_on_compute("newbox", "echo hi", scheduler="slurm"),
        lambda: ssh_hpc_server.submit_job("newbox", "#!/bin/bash\n", scheduler="slurm"),
    ])
    def test_command_tools_carry_it_on_first_use(self, mock_subprocess, call):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        assert "[first use of 'newbox'" in call()
