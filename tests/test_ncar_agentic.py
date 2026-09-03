"""Rules from NSF NCAR's own agentic-AI guidance, and the user-controlled override.

https://ncar-hpc-docs.readthedocs.io/en/latest/best-practices-for-supercomputer-users/agentic-ai/

That page is explicit about a class of damage this server could otherwise do
silently: recursive traversal at or above a shared root creates a metadata
storm that degrades GLADE for every user on the machine. It also caps build
parallelism, forbids chmod 777, and warns against tight polling loops.

The policy escape is deliberately not a tool parameter. A tier exists to stop
the agent doing something the human did not intend, so only the human can
relax it, by editing hosts.toml or launching the server with an environment
variable.
"""

import json

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import _classify_command, _policy_mode, execute_remote_bash


def tier(cmd, role="login"):
    return _classify_command(cmd, role)[0]


# ---------------------------------------------------------------------------
# Metadata storms
# ---------------------------------------------------------------------------

class TestSharedRootTraversal:
    @pytest.mark.parametrize("cmd", [
        "find / -name '*.nc'",
        "find /glade -name x",
        "find /glade/ -type f",
        "find /glade/u -name x",
        "find /glade/u/home -name x",
        "find /glade/work -name '*.log'",
        "find /glade/campaign -type d",
        "find /glade/derecho -name x",
        "find /glade/derecho/scratch -name x",
        "lfs find /glade/derecho/scratch -type f",
        "du -sh /glade/work",
        "du -h /glade/campaign/",
        "ls -R /glade",
        "ls -lR /glade/u/home",
        "tree /glade/work",
        "grep -r pattern /glade",
        "grep -rn TODO /glade/work",
        "rg pattern /glade/campaign",
        "ncdu /glade/derecho/scratch",
        "cd /tmp && find /glade/work -name x",
        "find /scratch -name x",
        "find /projects -type f",
        "find /pl/active -name x",
    ])
    def test_traversal_at_or_above_a_shared_root_is_blocked(self, cmd):
        assert tier(cmd) == "block", cmd

    @pytest.mark.parametrize("cmd", [
        "find /glade/work/me/run1 -name '*.nc'",
        "find /glade/derecho/scratch/me/run2 -maxdepth 2 -name x",
        "lfs find /glade/derecho/scratch/me/run1 -type f",
        "du -sh /glade/work/me/project",
        "grep -r TODO /glade/u/home/me/src",
        "ls -R /glade/work/me/run1",
        "rg pattern /glade/work/me/code",
        "find . -name '*.py'",
        "find ./src -name x",
        "du -sh .",
    ])
    def test_traversal_inside_your_own_subdirectory_is_allowed(self, cmd):
        assert tier(cmd) != "block", cmd

    def test_the_rule_names_the_reason(self):
        _, rule = _classify_command("du -sh /glade/work", "login")
        assert "metadata" in rule.lower() or "shared root" in rule.lower()

    def test_still_blocked_on_a_data_access_node(self):
        assert tier("find /glade -name x", "dtn") == "block"

    def test_a_non_hpc_host_is_not_policed_at_all(self, tmp_path, monkeypatch):
        """hpc=false turns the policy off, rather than being a role."""
        cfg = tmp_path / "config"
        cfg.write_text(json.dumps({"hosts": {"box": {"hpc": False}}}))
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(cfg))
        monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
        assert ssh_hpc_server._policy_mode("box") == "off"


# ---------------------------------------------------------------------------
# Other rules the NCAR page states outright
# ---------------------------------------------------------------------------

class TestNcarSpecificRules:
    @pytest.mark.parametrize("cmd", ["chmod 777 out.nc", "chmod -R 777 dir", "chmod a+rwx script.sh"])
    def test_world_writable_permissions_need_confirmation(self, cmd):
        assert tier(cmd) in ("confirm", "block"), cmd

    def test_ordinary_chmod_is_free(self):
        assert tier("chmod 644 notes.txt") == "free"
        assert tier("chmod +x run.sh") == "free"

    @pytest.mark.parametrize("cmd", ["make -j", "make -j all", "cmake --build . -- -j"])
    def test_unbounded_build_parallelism_is_flagged(self, cmd):
        assert tier(cmd) == "confirm", cmd

    @pytest.mark.parametrize("cmd", ["make -j4", "make -j 2", "make -j8 install"])
    def test_bounded_parallelism_is_only_routed(self, cmd):
        assert tier(cmd) == "route", cmd

    @pytest.mark.parametrize("cmd", ["tail -f run.log", "tail -F job.out", "watch -n1 qstat"])
    def test_polling_loops_are_routed_off_the_login_node(self, cmd):
        assert tier(cmd) == "route", cmd

    def test_plain_tail_is_free(self):
        assert tier("tail -n 100 run.log") == "free"


# ---------------------------------------------------------------------------
# The human-controlled policy escape
# ---------------------------------------------------------------------------

POLICY_SETTINGS = {
    "derecho": {"center": "ncar", "role": "login"},
    "my-box": {"hpc": False},
    "loose": {"role": "login", "policy": "permissive"},
}


@pytest.fixture
def policy_profiles(tmp_path, monkeypatch):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": POLICY_SETTINGS}))
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
    yield path


class TestPolicyMode:
    def test_default_is_strict(self, policy_profiles):
        assert _policy_mode("derecho") == "strict"

    def test_per_host_override(self, policy_profiles):
        assert _policy_mode("my-box") == "off"
        assert _policy_mode("loose") == "permissive"

    def test_environment_variable_overrides_everything(self, policy_profiles, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_POLICY", "off")
        assert _policy_mode("derecho") == "off"

    def test_unknown_mode_falls_back_to_strict(self, policy_profiles, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_POLICY", "yolo")
        assert _policy_mode("derecho") == "strict"

    def test_no_settings_at_all_is_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(tmp_path / "absent.json"))
        monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
        assert _policy_mode("anything") == "strict"


class TestPolicyModeChangesEnforcement:
    def test_off_lets_a_blocked_command_run(self, policy_profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="my-box", command="sudo ls")
        mock_subprocess.assert_called_once()

    def test_off_also_skips_the_confirm_tier(self, policy_profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="my-box", command="rm -rf /home/me/old")
        mock_subprocess.assert_called_once()

    def test_permissive_downgrades_block_to_confirm(self, policy_profiles, mock_subprocess):
        refusal = execute_remote_bash(host="loose", command="sudo ls")
        assert "confirm_destructive" in refusal
        mock_subprocess.assert_not_called()

        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="loose", command="sudo ls", confirm_destructive=True)
        mock_subprocess.assert_called_once()

    def test_strict_still_refuses_with_the_flag_set(self, policy_profiles, mock_subprocess):
        refusal = execute_remote_bash(
            host="derecho", command="sudo ls", confirm_destructive=True, allow_on_login_node=True,
        )
        assert "Blocked" in refusal
        mock_subprocess.assert_not_called()

    def test_strict_refusal_tells_the_human_how_to_relax_it(self, policy_profiles):
        refusal = execute_remote_bash(host="derecho", command="sudo ls")
        assert "permissive" in refusal
        assert "HPC_SSH_MCP_POLICY" in refusal
        assert "settings file" in refusal

    def test_the_refusal_does_not_point_the_model_at_a_tool(self, policy_profiles):
        """The escape belongs to the human. record_host would hand it to the
        model, so the refusal names the file and the env var, and nothing else."""
        refusal = execute_remote_bash(host="derecho", command="sudo ls")
        assert "record_host" not in refusal
        assert "do not do it for them" in refusal

    def test_the_model_cannot_change_the_mode_through_a_tool(self):
        """No tool takes a policy argument: the escape lives in config only."""
        import inspect
        for name in ("execute_remote_bash", "run_on_compute", "submit_job"):
            params = inspect.signature(getattr(ssh_hpc_server, name)).parameters
            assert not any("policy" in p for p in params), name
