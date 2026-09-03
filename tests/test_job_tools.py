"""Scheduler-aware job tools.

NCAR Derecho and Casper run PBS Pro (qsub/qstat/qdel, plus NCAR's qcmd);
CU Boulder Alpine runs Slurm. The tools detect the scheduler once per host
(or take scheduler="pbs"|"slurm" explicitly) and emit the right commands.
Every remote script is delivered on stdin to `bash -s`, so tests inspect
the `input` kwarg of subprocess.run.
"""

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _detect_scheduler,
    cancel_job,
    check_job,
    list_queue,
    run_on_compute,
    submit_job,
)

PROBE_PBS = make_completed_process(returncode=0, stdout="qsub\n")
PROBE_SLURM = make_completed_process(returncode=0, stdout="sbatch\n")
OK = make_completed_process(returncode=0, stdout="ok\n")


def _script(call):
    return call.kwargs["input"]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestSchedulerDetection:
    def test_pbs_host(self, mock_subprocess):
        mock_subprocess.return_value = PROBE_PBS
        assert _detect_scheduler("derecho") == "pbs"
        script = _script(mock_subprocess.call_args)
        assert "command -v" in script
        assert "qsub" in script and "sbatch" in script

    def test_slurm_host(self, mock_subprocess):
        mock_subprocess.return_value = PROBE_SLURM
        assert _detect_scheduler("cu-alpine") == "slurm"

    def test_result_is_cached_per_host(self, mock_subprocess):
        mock_subprocess.return_value = PROBE_PBS
        _detect_scheduler("derecho")
        _detect_scheduler("derecho")
        assert mock_subprocess.call_count == 1

    def test_no_scheduler_is_an_error(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        with pytest.raises(ValueError, match="No PBS or Slurm"):
            _detect_scheduler("venus")

    def test_both_schedulers_is_ambiguous(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="qsub\nsbatch\n")
        with pytest.raises(ValueError, match="scheduler="):
            _detect_scheduler("odd-host")

    def test_ssh_failure_surfaces_the_reauth_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Permission denied (publickey,keyboard-interactive).",
        )
        with pytest.raises(ValueError, match="ssh -fN derecho"):
            _detect_scheduler("derecho")

    def test_explicit_scheduler_skips_the_probe(self, mock_subprocess):
        mock_subprocess.return_value = OK
        list_queue(host="derecho", scheduler="pbs")
        assert mock_subprocess.call_count == 1
        assert "qstat" in _script(mock_subprocess.call_args)

    def test_unknown_scheduler_value_is_rejected(self):
        with pytest.raises(ValueError, match="scheduler"):
            list_queue(host="derecho", scheduler="lsf")


# ---------------------------------------------------------------------------
# submit_job
# ---------------------------------------------------------------------------

class TestSubmitJobPbs:
    def test_writes_script_then_qsubs(self, mock_subprocess):
        content = "#!/bin/bash\n#PBS -A UABC0001\n#PBS -l walltime=00:10:00\necho hi\n"
        mock_subprocess.side_effect = [
            PROBE_PBS,
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="2426690.desched1\n"),
        ]
        result = submit_job(host="derecho", job_script_content=content, remote_filename="run.pbs")
        assert "2426690.desched1" in result
        write_call = mock_subprocess.call_args_list[1]
        assert "cat > run.pbs" in write_call[0][0][-1]
        assert write_call.kwargs["input"] == content
        assert _script(mock_subprocess.call_args_list[2]).strip() == "qsub run.pbs"

    def test_remote_dir_is_created_and_submitted_from(self, mock_subprocess):
        mock_subprocess.side_effect = [
            PROBE_PBS,
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        submit_job(
            host="derecho", job_script_content="#!/bin/bash", remote_filename="run.pbs",
            remote_dir="/glade/derecho/scratch/u/run 1",
        )
        write_cmd = mock_subprocess.call_args_list[1][0][0][-1]
        assert write_cmd.startswith(
            "mkdir -p '/glade/derecho/scratch/u/run 1' && cd '/glade/derecho/scratch/u/run 1' && cat > run.pbs"
        )
        submit_script = _script(mock_subprocess.call_args_list[2]).strip()
        assert submit_script == "cd '/glade/derecho/scratch/u/run 1' && qsub run.pbs"

    def test_tilde_remote_dir_expands_to_home(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        submit_job(
            host="derecho", job_script_content="#!/bin/bash", remote_filename="run.pbs",
            remote_dir="~/jobs", scheduler="pbs",
        )
        assert '"$HOME"/jobs' in mock_subprocess.call_args_list[0][0][0][-1]

    def test_write_failure_stops_before_submit(self, mock_subprocess):
        mock_subprocess.side_effect = [
            PROBE_PBS,
            make_completed_process(returncode=1, stderr="Permission denied\n"),
        ]
        result = submit_job(host="derecho", job_script_content="#!/bin/bash")
        assert "Failed to write script" in result
        assert mock_subprocess.call_count == 2

    def test_default_filename_is_generated(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        submit_job(host="derecho", job_script_content="#!/bin/bash", scheduler="pbs")
        assert "claude_job_" in mock_subprocess.call_args_list[0][0][0][-1]

    @pytest.mark.parametrize("field", ["remote_filename", "remote_dir"])
    def test_rejects_dash_prefixed_names(self, field):
        with pytest.raises(ValueError, match="must not start with"):
            submit_job(host="derecho", job_script_content="#!/bin/bash", scheduler="pbs", **{field: "-evil"})


class TestSubmitJobSlurm:
    def test_uses_sbatch_with_option_terminator(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 12345\n"),
        ]
        result = submit_job(
            host="cu-alpine", job_script_content="#!/bin/bash\n#SBATCH -N 1",
            remote_filename="job.sh", scheduler="slurm",
        )
        assert "12345" in result
        assert mock_subprocess.call_count == 2
        assert _script(mock_subprocess.call_args_list[1]).strip() == "sbatch -- job.sh"


# ---------------------------------------------------------------------------
# check_job
# ---------------------------------------------------------------------------

class TestCheckJobPbs:
    def test_single_round_trip_including_finished_jobs(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="Job Id: 2426690.desched1\n    job_state = F\n    Exit_status = 0\n",
        )
        result = check_job(host="derecho", job_id="2426690.desched1", scheduler="pbs")
        assert mock_subprocess.call_count == 1
        script = _script(mock_subprocess.call_args)
        assert "qstat -x" in script
        assert "2426690.desched1" in script
        assert "job_state = F" in result

    @pytest.mark.parametrize("good", [
        "2426690", "2426690.desched1", "657237.chadmin", "12345[].desched1", "12345[3].desched1",
    ])
    def test_accepts_pbs_job_ids(self, mock_subprocess, good):
        mock_subprocess.return_value = OK
        check_job(host="derecho", job_id=good, scheduler="pbs")
        assert good in _script(mock_subprocess.call_args)

    @pytest.mark.parametrize("bad", ["12345; rm -rf /", "abc", "12345_0", "-x", ""])
    def test_rejects_invalid_pbs_job_ids(self, bad):
        with pytest.raises(ValueError, match="PBS job ID"):
            check_job(host="derecho", job_id=bad, scheduler="pbs")


class TestCheckJobSlurm:
    def test_single_round_trip_squeue_and_sacct(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="JobID|State\n12345_0|COMPLETED\n",
        )
        result = check_job(host="cu-alpine", job_id="12345_0", scheduler="slurm")
        assert mock_subprocess.call_count == 1
        script = _script(mock_subprocess.call_args)
        assert "squeue -j 12345_0" in script
        assert "sacct -j 12345_0" in script
        assert "COMPLETED" in result

    def test_finished_job_is_reported_not_errored(self, mock_subprocess):
        """squeue exits 1 for a job that already left the queue; that is a state, not a failure."""
        mock_subprocess.return_value = OK
        check_job(host="cu-alpine", job_id="12345", scheduler="slurm")
        assert "not in queue" in _script(mock_subprocess.call_args)

    @pytest.mark.parametrize("bad", ["123.desched1", "12345; rm -rf /", "-x"])
    def test_rejects_invalid_slurm_job_ids(self, bad):
        with pytest.raises(ValueError, match="Slurm job ID"):
            check_job(host="cu-alpine", job_id=bad, scheduler="slurm")


# ---------------------------------------------------------------------------
# list_queue
# ---------------------------------------------------------------------------

class TestListQueue:
    def test_pbs_defaults_to_current_user(self, mock_subprocess):
        mock_subprocess.return_value = OK
        list_queue(host="derecho", scheduler="pbs")
        assert 'qstat -w -u "$USER"' in _script(mock_subprocess.call_args)

    def test_pbs_named_user(self, mock_subprocess):
        mock_subprocess.return_value = OK
        list_queue(host="derecho", user="jsmith", scheduler="pbs")
        assert "qstat -w -u jsmith" in _script(mock_subprocess.call_args)

    def test_slurm_defaults_to_current_user(self, mock_subprocess):
        mock_subprocess.return_value = OK
        list_queue(host="cu-alpine", scheduler="slurm")
        assert 'squeue -u "$USER"' in _script(mock_subprocess.call_args)

    def test_rejects_invalid_username(self):
        with pytest.raises(ValueError, match="username"):
            list_queue(host="derecho", user="j; rm", scheduler="pbs")


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------

class TestCancelJob:
    def test_pbs_uses_qdel(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        cancel_job(host="derecho", job_id="2426690.desched1", scheduler="pbs")
        assert _script(mock_subprocess.call_args).strip() == "qdel 2426690.desched1"

    def test_slurm_uses_scancel(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        cancel_job(host="cu-alpine", job_id="12345_0", scheduler="slurm")
        assert _script(mock_subprocess.call_args).strip() == "scancel 12345_0"

    def test_auto_detects_before_cancelling(self, mock_subprocess):
        mock_subprocess.side_effect = [PROBE_PBS, make_completed_process(returncode=0)]
        cancel_job(host="derecho", job_id="2426690")
        assert _script(mock_subprocess.call_args_list[1]).strip() == "qdel 2426690"


# ---------------------------------------------------------------------------
# run_on_compute: the policy-compliant way to run something heavy
# ---------------------------------------------------------------------------

class TestRunOnCompute:
    def test_pbs_wraps_command_in_qcmd(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="done\n")
        result = run_on_compute(
            host="derecho", command="python analyze.py --all", account="UABC0001",
            walltime="00:20:00", scheduler="pbs",
        )
        assert "done" in result
        script = _script(mock_subprocess.call_args).strip()
        assert script.startswith("qcmd -A UABC0001 -l walltime=00:20:00")
        assert script.endswith("-- bash -c 'python analyze.py --all'")
        assert mock_subprocess.call_args.kwargs["timeout"] == 1800

    def test_pbs_queue_and_resources(self, mock_subprocess):
        mock_subprocess.return_value = OK
        run_on_compute(
            host="casper", command="ncl plot.ncl", account="UABC0001", queue="casper",
            resources="select=1:ncpus=4:mem=16GB", scheduler="pbs",
        )
        script = _script(mock_subprocess.call_args)
        assert "-q casper" in script
        assert "-l select=1:ncpus=4:mem=16GB" in script

    def test_slurm_wraps_command_in_srun(self, mock_subprocess):
        mock_subprocess.return_value = OK
        run_on_compute(
            host="cu-alpine", command="python analyze.py", account="ucb-general",
            walltime="00:20:00", queue="amilan", resources="qos=normal,ntasks=4",
            scheduler="slurm",
        )
        script = _script(mock_subprocess.call_args).strip()
        assert script.startswith("srun --account=ucb-general --partition=amilan --qos=normal --ntasks=4 --time=00:20:00")
        assert script.endswith("bash -c 'python analyze.py'")

    def test_custom_timeout_is_honoured(self, mock_subprocess):
        mock_subprocess.return_value = OK
        run_on_compute(host="derecho", command="true", scheduler="pbs", timeout=90)
        assert mock_subprocess.call_args.kwargs["timeout"] == 90

    @pytest.mark.parametrize("kwargs", [
        {"account": "x; rm -rf /"},
        {"walltime": "1h"},
        {"queue": "main && reboot"},
        {"resources": "select=1; rm -rf /"},
    ])
    def test_rejects_unsafe_directive_values(self, kwargs):
        with pytest.raises(ValueError):
            run_on_compute(host="derecho", command="true", scheduler="pbs", **kwargs)
