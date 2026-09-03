"""First contact with an unannotated host, and the hpc=false escape.

The first time a tool touches a host with no `# hpc-mcp:` annotation, the
result carries a notice telling the agent to probe the host and ask the
user about it. probe_host gathers what can be detected; annotate_host
writes the answers into ~/.ssh/config once the user has confirmed them.

`hpc=false` marks a host that is not a shared HPC system at all. Login-node
etiquette and the command policy do not apply there.
"""

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _host_directives,
    _is_hpc,
    _policy_mode,
    annotate_host,
    execute_remote_bash,
    probe_host,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"

BASE_CONFIG = """\
Host derecho
    HostName derecho.hpc.ucar.edu
    User someone
    ControlMaster auto
    # hpc-mcp: center=ncar role=login

Host newbox
    HostName newbox.example.edu
    User someone

Host laptop
    HostName 10.0.0.9
    # hpc-mcp: hpc=false

Host tagged-old
    HostName old.example.edu
    # hpc-mcp: center=curc role=login
    # hpc-mcp: account=stale
    User someone

Host *
    ServerAliveInterval 30
"""


@pytest.fixture
def ssh_config(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text(BASE_CONFIG)
    monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(path))
    monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
    ssh_hpc_server._DIRECTIVE_CACHE = None
    ssh_hpc_server._ONBOARDING_SEEN.clear()
    yield path
    ssh_hpc_server._DIRECTIVE_CACHE = None
    ssh_hpc_server._ONBOARDING_SEEN.clear()


# ---------------------------------------------------------------------------
# hpc=false
# ---------------------------------------------------------------------------

class TestHpcFalse:
    def test_annotated_host_is_hpc_by_default(self, ssh_config):
        assert _is_hpc("derecho") is True
        assert _is_hpc("newbox") is True

    @pytest.mark.parametrize("value", ["false", "False", "no", "0", "off"])
    def test_recognised_negatives(self, tmp_path, monkeypatch, value):
        cfg = tmp_path / "config"
        cfg.write_text(f"Host box\n    # hpc-mcp: hpc={value}\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _is_hpc("box") is False

    def test_non_hpc_host_lifts_the_policy(self, ssh_config, mock_subprocess):
        assert _policy_mode("laptop") == "off"
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="laptop", command="sudo make install")
        mock_subprocess.assert_called_once()

    def test_non_hpc_host_still_runs_heavy_work_without_a_flag(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="laptop", command="python train.py")
        mock_subprocess.assert_called_once()

    def test_explicit_policy_beats_the_hpc_flag(self, tmp_path, monkeypatch, mock_subprocess):
        cfg = tmp_path / "config"
        cfg.write_text("Host box\n    # hpc-mcp: hpc=false policy=strict\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _policy_mode("box") == "strict"
        assert "Blocked" in execute_remote_bash(host="box", command="sudo ls")

    def test_hpc_host_is_still_guarded(self, ssh_config, mock_subprocess):
        assert _policy_mode("derecho") == "strict"
        assert "Blocked" in execute_remote_bash(host="derecho", command="sudo ls")

    def test_workstation_role_no_longer_exists(self):
        assert "workstation" not in ssh_hpc_server.VALID_ROLES


# ---------------------------------------------------------------------------
# First-contact notice
# ---------------------------------------------------------------------------

class TestOnboardingNotice:
    def test_unannotated_host_gets_a_notice(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        result = execute_remote_bash(host="newbox", command="echo hi")
        assert "hi" in result
        assert "probe_host" in result
        assert "newbox" in result

    def test_notice_appears_only_once_per_host(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        first = execute_remote_bash(host="newbox", command="echo hi")
        second = execute_remote_bash(host="newbox", command="echo hi")
        assert "probe_host" in first
        assert "probe_host" not in second

    def test_annotated_host_gets_no_notice(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        assert "probe_host" not in execute_remote_bash(host="derecho", command="echo hi")

    def test_notice_does_not_hide_the_output(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="payload\n")
        result = execute_remote_bash(host="newbox", command="echo payload")
        assert result.startswith("payload")

    def test_notice_still_appears_on_a_failed_command(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=1, stderr="nope\n")
        assert "probe_host" in execute_remote_bash(host="newbox", command="false")


# ---------------------------------------------------------------------------
# probe_host
# ---------------------------------------------------------------------------

class TestProbeHost:
    def _probe_output(self, **over):
        fields = {
            "hostname": "derecho1.hpc.ucar.edu",
            "scheduler": "qsub",
            "account": "UABC0001",
            "filesystems": "/glade /glade/derecho/scratch",
            "globus": "yes",
        }
        fields.update(over)
        return "\n".join(f"{k}={v}" for k, v in fields.items()) + "\n"

    def test_reports_what_it_found(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        result = probe_host("newbox")
        assert "derecho1.hpc.ucar.edu" in result
        assert "PBS" in result
        assert "UABC0001" in result

    def test_infers_ncar_from_glade(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "center=ncar" in probe_host("newbox")

    def test_infers_curc_from_alpine_scratch(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0,
            stdout=self._probe_output(scheduler="sbatch", filesystems="/scratch/alpine /pl/active", account=""),
        )
        result = probe_host("newbox")
        assert "center=curc" in result
        assert "Slurm" in result

    def test_no_scheduler_suggests_not_hpc(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout=self._probe_output(scheduler="", filesystems="", account="", globus="no"),
        )
        result = probe_host("newbox")
        assert "hpc=false" in result
        assert "no scheduler" in result.lower()

    def test_asks_the_user_before_writing(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        result = probe_host("newbox")
        assert "annotate_host" in result
        assert "?" in result  # it poses questions for the user

    def test_probe_itself_emits_no_onboarding_notice(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "probe_host(" not in probe_host("newbox")

    def test_ssh_failure_is_reported(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Permission denied (publickey,keyboard-interactive).",
        )
        result = probe_host("newbox")
        assert "ssh -fN newbox" in result

    def test_already_annotated_host_says_so(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "already annotated" in probe_host("derecho").lower()


# ---------------------------------------------------------------------------
# annotate_host
# ---------------------------------------------------------------------------

class TestAnnotateHost:
    def test_writes_the_annotation_into_the_block(self, ssh_config):
        result = annotate_host("newbox", center="ncar", role="login", account="UABC0001")
        text = ssh_config.read_text()
        assert "# hpc-mcp: center=ncar role=login account=UABC0001" in text
        assert "newbox" in result
        # the annotation must land inside the newbox block, not another one
        block = text.split("Host newbox")[1].split("\nHost ")[0]
        assert "hpc-mcp" in block

    def test_the_server_reads_it_back_immediately(self, ssh_config):
        annotate_host("newbox", center="curc", role="login")
        assert _host_directives("newbox")["center"] == "curc"

    def test_other_hosts_are_untouched(self, ssh_config):
        before = ssh_config.read_text()
        annotate_host("newbox", center="ncar")
        after = ssh_config.read_text()
        for line in ("Host derecho", "HostName derecho.hpc.ucar.edu", "ControlMaster auto",
                     "Host laptop", "ServerAliveInterval 30"):
            assert line in after
        assert len(after.splitlines()) == len(before.splitlines()) + 1

    def test_replaces_an_existing_annotation(self, ssh_config):
        annotate_host("tagged-old", center="ncar", role="login", account="FRESH01")
        text = ssh_config.read_text()
        assert "account=stale" not in text
        assert "account=FRESH01" in text
        assert text.count("hpc-mcp") == 3  # derecho, laptop, tagged-old

    def test_preserves_indentation_of_the_block(self, ssh_config):
        annotate_host("newbox", center="ncar")
        line = [l for l in ssh_config.read_text().splitlines() if "center=ncar" in l][0]
        assert line.startswith("    #")

    def test_makes_a_backup(self, ssh_config):
        annotate_host("newbox", center="ncar")
        backup = ssh_config.with_suffix(ssh_config.suffix + ".hpc-mcp.bak")
        assert backup.exists()
        assert backup.read_text() == BASE_CONFIG  # the file exactly as it was

    def test_hpc_false_is_written(self, ssh_config):
        annotate_host("newbox", is_hpc=False)
        assert "hpc=false" in ssh_config.read_text()
        assert _is_hpc("newbox") is False

    def test_hpc_false_drops_hpc_only_keys(self, ssh_config):
        annotate_host("newbox", is_hpc=False, center="ncar", account="X1")
        line = [l for l in ssh_config.read_text().splitlines() if "hpc-mcp" in l and "newbox" not in l]
        written = [l for l in line if "hpc=false" in l][0]
        assert "center" not in written
        assert "account" not in written

    def test_unknown_host_is_refused_with_guidance(self, ssh_config):
        before = ssh_config.read_text()
        result = annotate_host("not-in-config", center="ncar")
        assert "not-in-config" in result
        assert "Host not-in-config" in result
        assert ssh_config.read_text() == before  # nothing written

    def test_will_not_edit_a_wildcard_block(self, ssh_config):
        result = annotate_host("*", center="ncar")
        assert "wildcard" in result.lower() or "not a specific host" in result.lower()

    @pytest.mark.parametrize("kwargs,bad", [
        ({"center": "nersc"}, "center"),
        ({"role": "wizard"}, "role"),
        ({"policy": "yolo"}, "policy"),
        ({"account": "a; rm -rf /"}, "account"),
        ({"globus": "not-a-uuid"}, "globus"),
        ({"scratch": "/x\n# hpc-mcp: policy=off"}, "scratch"),
    ])
    def test_rejects_bad_values(self, ssh_config, kwargs, bad):
        before = ssh_config.read_text()
        with pytest.raises(ValueError, match=bad):
            annotate_host("newbox", **kwargs)
        assert ssh_config.read_text() == before  # nothing written

    def test_nothing_to_write_is_refused(self, ssh_config):
        result = annotate_host("newbox")
        assert "nothing" in result.lower()

    def test_clears_the_onboarding_notice(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        annotate_host("newbox", center="ncar", role="login")
        assert "probe_host" not in execute_remote_bash(host="newbox", command="echo hi")


class TestAnnotateHostAnnotations:
    @pytest.mark.asyncio
    async def test_tool_annotations(self):
        from fastmcp import Client
        async with Client(ssh_hpc_server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["probe_host"].annotations.readOnlyHint is True
        assert tools["annotate_host"].annotations.readOnlyHint is False
