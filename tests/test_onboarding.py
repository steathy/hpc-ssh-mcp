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

Host plainer
    HostName plainer.example.edu

Host laptop
    HostName 10.0.0.9
    # hpc-mcp: hpc=false

Host *
    ServerAliveInterval 30
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "store" / "hosts.json"
    path.parent.mkdir()
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    ssh_hpc_server._DIRECTIVE_CACHE = None
    yield path
    ssh_hpc_server._DIRECTIVE_CACHE = None


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

class TestAnnotateHostNeverTouchesSshConfig:
    """~/.ssh/config controls access to every host the user has. This server
    reads it and never writes to it: a mangled line, a replaced symlink or a
    downgraded file mode would cost the user far more than the convenience is
    worth. Annotations are written to a small file this server owns instead,
    which can be deleted at any time with no consequence but lost defaults."""

    def test_ssh_config_is_left_byte_for_byte_identical(self, ssh_config, store):
        before = ssh_config.read_bytes()
        annotate_host("newbox", center="ncar", role="login", account="UABC0001")
        assert ssh_config.read_bytes() == before

    def test_no_backup_or_temp_file_is_left_beside_the_ssh_config(self, ssh_config, store):
        annotate_host("newbox", center="ncar")
        derived = [p.name for p in ssh_config.parent.iterdir()
                   if p.is_file() and p.name.startswith("config") and p.name != "config"]
        assert derived == []

    def test_a_symlinked_ssh_config_survives(self, tmp_path, monkeypatch, store):
        real = tmp_path / "dotfiles" / "ssh_config"
        real.parent.mkdir()
        real.write_text(BASE_CONFIG)
        link = tmp_path / "config"
        link.symlink_to(real)
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(link))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        annotate_host("newbox", center="ncar")
        assert link.is_symlink()
        assert real.read_text() == BASE_CONFIG

    def test_ssh_config_permissions_are_untouched(self, ssh_config, store):
        ssh_config.chmod(0o600)
        annotate_host("newbox", center="ncar")
        assert oct(ssh_config.stat().st_mode)[-3:] == "600"


class TestAnnotateHostStore:
    def test_writes_the_annotation_to_the_store(self, ssh_config, store):
        result = annotate_host("newbox", center="ncar", role="login", account="UABC0001")
        assert "newbox" in result
        assert str(store) in result
        assert store.exists()
        assert _host_directives("newbox") == {
            "center": "ncar", "role": "login", "account": "UABC0001",
        }

    def test_the_server_reads_it_back_immediately(self, ssh_config, store):
        annotate_host("newbox", center="curc", role="login")
        assert _host_directives("newbox")["center"] == "curc"

    def test_store_is_created_with_parent_directories(self, ssh_config, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "deeper" / "hosts.conf"
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(target))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        annotate_host("newbox", center="ncar")
        assert target.exists()

    def test_store_is_private(self, ssh_config, store):
        annotate_host("newbox", center="ncar")
        assert oct(store.stat().st_mode)[-3:] == "600"

    def test_store_explains_itself(self, ssh_config, store):
        annotate_host("newbox", center="ncar")
        assert "delete" in store.read_text().lower()

    def test_re_annotating_updates_without_dropping_the_rest(self, ssh_config, store):
        annotate_host("newbox", center="ncar", account="OLD001")
        annotate_host("newbox", account="NEW002")
        directives = _host_directives("newbox")
        assert directives["account"] == "NEW002"
        assert directives["center"] == "ncar"
        assert "OLD001" not in store.read_text()

    def test_other_entries_survive(self, ssh_config, store):
        annotate_host("newbox", center="ncar")
        annotate_host("plainer", center="curc")
        assert _host_directives("newbox")["center"] == "ncar"
        assert _host_directives("plainer")["center"] == "curc"

    def test_deleting_the_store_restores_defaults(self, ssh_config, store):
        annotate_host("newbox", center="curc")
        assert _host_directives("newbox")["center"] == "curc"
        store.unlink()
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("newbox") == {}

    def test_a_corrupt_store_is_ignored_not_fatal(self, ssh_config, store):
        store.write_text("this is not: a valid = anything\n\x00garbage\n")
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("newbox") == {}

    def test_hpc_false_is_written(self, ssh_config, store):
        annotate_host("newbox", is_hpc=False)
        assert _is_hpc("newbox") is False

    def test_hpc_false_drops_hpc_only_keys(self, ssh_config, store):
        annotate_host("newbox", is_hpc=False, center="ncar", account="X1")
        assert set(_host_directives("newbox")) == {"hpc"}


class TestAnnotateHostPrecedence:
    def test_a_hand_written_ssh_config_annotation_wins(self, ssh_config, store):
        """The user's own statement in ~/.ssh/config beats anything written here."""
        annotate_host("derecho", center="curc", role="compute")
        assert _host_directives("derecho")["center"] == "ncar"
        assert _host_directives("derecho")["role"] == "login"

    def test_and_says_so(self, ssh_config, store):
        result = annotate_host("derecho", center="curc")
        assert "~/.ssh/config" in result or "ssh config" in result.lower()
        assert "wins" in result.lower() or "takes precedence" in result.lower()

    def test_store_fills_keys_the_ssh_config_omits(self, ssh_config, store):
        annotate_host("derecho", center="ncar", role="login", account="UABC0001")
        assert _host_directives("derecho")["account"] == "UABC0001"


class TestAnnotateHostValidation:
    def test_unknown_host_is_flagged_but_still_recorded(self, ssh_config, store):
        result = annotate_host("not-in-config", center="ncar")
        assert "not-in-config" in result
        assert "no Host block" in result or "not in" in result.lower()

    def test_will_not_take_a_wildcard(self, ssh_config, store):
        result = annotate_host("*", center="ncar")
        assert "wildcard" in result.lower() or "not a specific host" in result.lower()
        assert not store.exists() or "*" not in store.read_text()

    @pytest.mark.parametrize("kwargs,bad", [
        ({"center": "nersc"}, "center"),
        ({"role": "wizard"}, "role"),
        ({"policy": "yolo"}, "policy"),
        ({"account": "a; rm -rf /"}, "account"),
        ({"globus": "not-a-uuid"}, "globus"),
        ({"scratch": "/x\n# hpc-mcp: policy=off"}, "scratch"),
    ])
    def test_rejects_bad_values(self, ssh_config, store, kwargs, bad):
        with pytest.raises(ValueError, match=bad):
            annotate_host("newbox", **kwargs)
        assert not store.exists()

    def test_nothing_to_write_is_refused(self, ssh_config, store):
        result = annotate_host("newbox")
        assert "nothing" in result.lower()
        assert not store.exists()

    def test_clears_the_onboarding_notice(self, ssh_config, store, mock_subprocess):
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
