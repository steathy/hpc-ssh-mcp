"""Command policy: what execute_remote_bash refuses, asks about, or reroutes.

Four tiers, checked in order:
  block   never runs (privilege escalation, root wipes, key persistence, ...)
  confirm runs only with confirm_destructive=True (recursive deletes, force pushes, ...)
  route   on a login node runs only with allow_on_login_node=True (compute, builds, big I/O)
  free    runs

The classifier is a pure function so the rules can be tested exhaustively
without a mock. Tool-level tests check the flags and the messages.
"""

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import _classify_command, execute_remote_bash, run_on_compute


def tier(cmd, role="login"):
    return _classify_command(cmd, role)[0]


# ---------------------------------------------------------------------------
# Block tier
# ---------------------------------------------------------------------------

class TestBlockTier:
    @pytest.mark.parametrize("cmd", [
        "sudo apt install foo",
        "ls; sudo -i",
        "su -",
        "echo x | su root",
        "apt-get install -y gcc",
        "yum install python3",
        "dnf remove vim",
        "rm -rf /",
        "rm -rf /*",
        "rm -fr ~",
        "rm -rf ~/",
        "rm -rf $HOME",
        "rm -rf \"$HOME\"/",
        "rm -rf ${HOME}",
        "rm -rf /glade",
        "rm -rf /glade/scratch/",
        "rm -rf /glade/work",
        "rm -rf /scratch/alpine",
        "rm -rf /pl/active/",
        "rm -rf /projects",
        "rm -rf /home",
        "cd /glade/u/home/me && rm -rf -- /",
        "rm -rf --no-preserve-root /",
        ":(){ :|:& };:",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "echo ssh-ed25519 AAAA >> ~/.ssh/authorized_keys",
        "cat key.pub > /home/u/.ssh/authorized_keys",
        "cat key.pub | tee -a ~/.ssh/authorized_keys",
        "sed -i 's/x/y/' ~/.ssh/authorized_keys",
    ])
    def test_blocked(self, cmd):
        assert tier(cmd) == "block", cmd

    def test_block_applies_on_every_role(self):
        for role in ("login", "dtn", "compute", "workstation"):
            assert tier("sudo ls", role) == "block"

    def test_block_has_no_override(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x")
        result = execute_remote_bash(
            host="derecho", command="sudo ls", confirm_destructive=True, allow_on_login_node=True,
        )
        assert "Blocked" in result
        assert "sudo" in result
        mock_subprocess.assert_not_called()

    def test_block_applies_to_run_on_compute(self, mock_subprocess):
        result = run_on_compute(host="derecho", command="sudo make install", scheduler="pbs")
        assert "Blocked" in result
        mock_subprocess.assert_not_called()


# ---------------------------------------------------------------------------
# Confirm tier
# ---------------------------------------------------------------------------

class TestConfirmTier:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /glade/scratch/me/run1",
        "rm -r old_outputs",
        "rm -Rf build/",
        "find . -name '*.tmp' -delete",
        "find /scratch/alpine/me -mtime +30 -exec rm {} \\;",
        "chmod -R 777 /glade/work/me/shared",
        "chown -R me:grp data/",
        "git push --force origin main",
        "git push -f",
        "git reset --hard HEAD~3",
        "git clean -fdx",
        "git branch -D feature",
        "scancel -u $USER",
        "scancel --user=me",
        "qdel $(qselect -u $USER)",
        "qdel `qselect -u me`",
        "truncate -s 0 model.log",
        "shred -u secrets.txt",
        "crontab -e",
        "crontab mycron.txt",
        "ssh-keygen -t ed25519",
        "python merge.py > combined.nc",
        "ncrcat a.nc b.nc >> all.nc",
        "dd if=big.bin of=copy.bin",
    ])
    def test_needs_confirmation(self, cmd):
        assert tier(cmd) == "confirm", cmd

    def test_refused_without_flag_and_message_names_the_flag(self, mock_subprocess):
        result = execute_remote_bash(host="derecho", command="rm -rf /glade/scratch/me/run1")
        assert "confirm_destructive" in result
        assert "rm" in result
        mock_subprocess.assert_not_called()

    def test_runs_with_flag(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        execute_remote_bash(host="derecho", command="rm -rf /glade/scratch/me/run1", confirm_destructive=True)
        assert mock_subprocess.call_args.kwargs["input"] == "rm -rf /glade/scratch/me/run1"

    def test_run_on_compute_honours_confirm_tier(self, mock_subprocess):
        result = run_on_compute(host="derecho", command="rm -rf run1", scheduler="pbs")
        assert "confirm_destructive" in result
        mock_subprocess.assert_not_called()
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        run_on_compute(host="derecho", command="rm -rf run1", scheduler="pbs", confirm_destructive=True)
        mock_subprocess.assert_called_once()


# ---------------------------------------------------------------------------
# Route tier: heavy work does not belong on a login node
# ---------------------------------------------------------------------------

class TestRouteTier:
    @pytest.mark.parametrize("cmd", [
        "python analyze.py",
        "python3 -m mypkg.train --epochs 5",
        "cd /glade/work/me && python3.11 run_model.py",
        "Rscript fit.R",
        "julia solve.jl",
        "ncl plot.ncl",
        "matlab -batch run_all",
        "cdo yearmean in.nc out.nc",
        "ncks -v tas in.nc out.nc",
        "ncra *.nc mean.nc",
        "make -j8",
        "cmake --build .",
        "gfortran -O2 model.f90",
        "mpicc hello.c -o hello",
        "nvcc kernel.cu",
        "mpiexec -n 4 ./a.out",
        "mpirun -np 8 ./model",
        "tar czf run1.tgz run1/",
        "tar -xf big.tar",
        "zip -r out.zip out/",
        "unzip archive.zip",
        "rsync -av /glade/scratch/me/run1 dtn:/dest/",
        "conda create -n env python=3.12",
        "conda install -c conda-forge xarray",
        "mamba env create -f env.yml",
        "pip install torch",
        "nohup ./long_job.sh &",
        "setsid python server.py",
        "jupyter lab --no-browser",
        "dask-scheduler",
    ])
    def test_routed_on_login_node(self, cmd):
        assert tier(cmd, "login") == "route", cmd

    @pytest.mark.parametrize("cmd", [
        "python --version",
        "python -c 'import xarray; print(xarray.__version__)'",
        "python3 -c \"print(1)\"",
        "which python",
        "module load conda && conda env list",
        "conda activate npl && python -c 'import numpy'",
        "pip list",
        "pip show xarray",
        "make --version",
        "gfortran --version",
        "ls -la /glade/scratch/me",
        "cat run.log | tail -20",
        "grep -c ERROR run.log",
        "qstat -u $USER",
        "squeue -u $USER",
        "gladequota",
        "git status && git log --oneline -5",
        "head -n 50 out.txt",
        "ncdump -h data.nc",
        "df -h /glade/scratch",
        "module avail 2>&1 | head",
        "echo 'import x' > script.py",
        "tar tzf archive.tgz",
    ])
    def test_light_work_is_free(self, cmd):
        assert tier(cmd, "login") == "free", cmd

    def test_route_does_not_apply_on_compute_or_workstation(self):
        assert tier("python analyze.py", "compute") == "free"
        assert tier("make -j8", "workstation") == "free"

    def test_dtn_allows_transfers_but_not_compute(self):
        assert tier("rsync -av src/ dest/", "dtn") == "free"
        assert tier("tar czf x.tgz dir/", "dtn") == "free"
        assert tier("python analyze.py", "dtn") == "route"

    def test_refused_with_pointer_to_run_on_compute(self, mock_subprocess):
        result = execute_remote_bash(host="derecho", command="python analyze.py")
        assert "run_on_compute" in result
        assert "allow_on_login_node" in result
        assert "login node" in result
        mock_subprocess.assert_not_called()

    def test_runs_with_override(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        execute_remote_bash(host="derecho", command="python analyze.py", allow_on_login_node=True)
        mock_subprocess.assert_called_once()

    def test_multiline_script_is_judged_by_its_worst_line(self):
        script = "cd /glade/work/me\nls\npython train.py\n"
        assert tier(script) == "route"
        script = "cd /glade/work/me\nrm -rf run1\npython train.py\n"
        assert tier(script) == "confirm"

    def test_classification_names_the_rule(self):
        t, rule = _classify_command("sudo ls", "login")
        assert t == "block" and "sudo" in rule
        t, rule = _classify_command("python train.py", "login")
        assert t == "route" and "python" in rule.lower()
