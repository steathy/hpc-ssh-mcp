"""Host profiles, scheduler-poll rate limiting, and the bulk-transfer guard.

A profile file (TOML) keyed by SSH alias tells the server what a host is,
so it can pick PBS or Slurm without probing, default the account, know that
a DTN is for transfers, and point at the right scratch filesystem:

    [derecho]
    center  = "ncar"
    role    = "login"
    account = "UABC0001"
    scratch = "/glade/derecho/scratch/$USER"
"""

import time

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _host_profile,
    _host_role,
    check_job,
    execute_remote_bash,
    list_queue,
    run_on_compute,
    scp_upload_file,
    submit_job,
)

PROFILE_TOML = """
[derecho]
center  = "ncar"
role    = "login"
account = "UABC0001"
scratch = "/glade/derecho/scratch/$USER"

[ncar-data]
center = "ncar"
role   = "data-access"

[cu-alpine]
center  = "curc"
role    = "login"
account = "ucb-general"
scratch = "/scratch/alpine/$USER"

[my-box]
role = "workstation"
"""


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    path = tmp_path / "hosts.toml"
    path.write_text(PROFILE_TOML)
    monkeypatch.setenv("HPC_SSH_MCP_CONFIG", str(path))
    ssh_hpc_server._PROFILE_CACHE = None
    yield path
    ssh_hpc_server._PROFILE_CACHE = None


class TestProfileLoading:
    def test_reads_fields(self, profiles):
        p = _host_profile("derecho")
        assert p["center"] == "ncar"
        assert p["role"] == "login"
        assert p["account"] == "UABC0001"
        assert p["scratch"] == "/glade/derecho/scratch/$USER"

    def test_unknown_host_gets_safe_defaults(self, profiles):
        assert _host_profile("some-other-host") == {}
        assert _host_role("some-other-host") == "login"

    def test_missing_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_CONFIG", str(tmp_path / "nope.toml"))
        ssh_hpc_server._PROFILE_CACHE = None
        assert _host_profile("derecho") == {}

    def test_malformed_file_is_not_an_error(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.toml"
        bad.write_text("this is not = valid = toml [[[")
        monkeypatch.setenv("HPC_SSH_MCP_CONFIG", str(bad))
        ssh_hpc_server._PROFILE_CACHE = None
        assert _host_profile("derecho") == {}

    def test_roles_drive_the_policy_tier(self, profiles):
        assert _host_role("derecho") == "login"
        assert _host_role("ncar-data") == "dtn"
        assert _host_role("my-box") == "workstation"

    def test_workstation_role_lifts_login_node_routing(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="my-box", command="python train.py")
        mock_subprocess.assert_called_once()

    def test_data_access_role_allows_transfers_but_not_compute(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="ncar-data", command="rsync -av /glade/scratch/me/run1 /dest/")
        mock_subprocess.assert_called_once()
        assert "run_on_compute" in execute_remote_bash(host="ncar-data", command="python train.py")


class TestProfileDrivesScheduler:
    def test_ncar_center_means_pbs_without_probing(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        list_queue(host="derecho")
        assert mock_subprocess.call_count == 1  # no `command -v` probe
        assert "qstat" in mock_subprocess.call_args.kwargs["input"]

    def test_curc_center_means_slurm_without_probing(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        list_queue(host="cu-alpine")
        assert mock_subprocess.call_count == 1
        assert "squeue" in mock_subprocess.call_args.kwargs["input"]

    def test_explicit_scheduler_still_wins(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        list_queue(host="derecho", scheduler="slurm")
        assert "squeue" in mock_subprocess.call_args.kwargs["input"]

    def test_unprofiled_host_still_probes(self, profiles, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="sbatch\n"),
            make_completed_process(returncode=0, stdout=""),
        ]
        list_queue(host="unknown-cluster")
        assert mock_subprocess.call_count == 2


class TestProfileDrivesAccount:
    def test_run_on_compute_uses_the_profile_account(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        run_on_compute(host="derecho", command="true")
        assert "-A UABC0001" in mock_subprocess.call_args.kwargs["input"]

    def test_explicit_account_wins(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        run_on_compute(host="derecho", command="true", account="OTHER001")
        assert "-A OTHER001" in mock_subprocess.call_args.kwargs["input"]

    def test_no_profile_means_no_account_flag(self, profiles, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="qsub\n"),
            make_completed_process(returncode=0, stdout="ok"),
        ]
        run_on_compute(host="unknown-cluster", command="true")
        assert "-A " not in mock_subprocess.call_args.kwargs["input"]


class TestSchedulerPollRateLimit:
    def test_repeat_query_within_window_is_served_from_cache(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="JOBID STATE\n1 R\n")
        first = list_queue(host="derecho")
        second = list_queue(host="derecho")
        assert mock_subprocess.call_count == 1
        assert "1 R" in second
        assert "cached" in second.lower()
        assert "cached" not in first.lower()

    def test_different_hosts_are_tracked_separately(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x\n")
        list_queue(host="derecho")
        list_queue(host="cu-alpine")
        assert mock_subprocess.call_count == 2

    def test_different_job_ids_are_tracked_separately(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x\n")
        check_job(host="derecho", job_id="1")
        check_job(host="derecho", job_id="2")
        assert mock_subprocess.call_count == 2

    def test_cache_expires(self, profiles, mock_subprocess, monkeypatch):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x\n")
        now = [1000.0]
        monkeypatch.setattr(ssh_hpc_server.time, "monotonic", lambda: now[0])
        list_queue(host="derecho")
        now[0] += ssh_hpc_server.SCHEDULER_POLL_INTERVAL + 1
        list_queue(host="derecho")
        assert mock_subprocess.call_count == 2

    def test_mutating_tools_are_never_cached(self, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import cancel_job
        cancel_job(host="derecho", job_id="1")
        cancel_job(host="derecho", job_id="1")
        assert mock_subprocess.call_count == 2


class TestBulkTransferGuard:
    def test_large_upload_is_flagged_with_alternatives(self, profiles, mock_subprocess, tmp_path):
        big = tmp_path / "big.nc"
        big.write_bytes(b"0")
        monkey_size = ssh_hpc_server.LARGE_TRANSFER_BYTES + 1

        import os
        real_getsize = os.path.getsize
        try:
            os.path.getsize = lambda p: monkey_size if str(p) == str(big) else real_getsize(p)
            mock_subprocess.return_value = make_completed_process(returncode=0)
            result = scp_upload_file(host="derecho", local_path=str(big), remote_path="/glade/x.nc")
        finally:
            os.path.getsize = real_getsize

        assert "Globus" in result
        assert mock_subprocess.called  # a warning, not a refusal

    def test_small_upload_is_not_flagged(self, profiles, mock_subprocess, tmp_path):
        small = tmp_path / "small.txt"
        small.write_text("hi")
        mock_subprocess.return_value = make_completed_process(returncode=0)
        assert "Globus" not in scp_upload_file(host="derecho", local_path=str(small), remote_path="/r/x")

    def test_missing_local_file_is_reported_before_scp_runs(self, profiles, mock_subprocess, tmp_path):
        result = scp_upload_file(host="derecho", local_path=str(tmp_path / "nope.nc"), remote_path="/r/x")
        assert "not found" in result.lower()
        mock_subprocess.assert_not_called()


class TestScratchHint:
    def test_submit_without_remote_dir_mentions_the_profile_scratch(self, profiles, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        result = submit_job(host="derecho", job_script_content="#!/bin/bash")
        assert "/glade/derecho/scratch/$USER" in result

    def test_submit_with_remote_dir_has_no_hint(self, profiles, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        result = submit_job(
            host="derecho", job_script_content="#!/bin/bash", remote_dir="/glade/derecho/scratch/me/run1",
        )
        assert "scratch" not in result.replace("/glade/derecho/scratch/me/run1", "")
