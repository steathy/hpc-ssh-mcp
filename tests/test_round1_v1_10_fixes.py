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
