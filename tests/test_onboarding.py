"""First contact with a host nothing is recorded for, and the hpc=false escape.

The first time a tool touches such a host, the result carries a notice telling
the agent to probe it and ask the user about it. probe_host gathers what can be
detected; record_host writes the answers to this server's own settings file
once the user has confirmed them. ~/.ssh/config is neither written nor read.

`hpc=false` marks a host that is not a shared HPC system at all. Login-node
etiquette and the command policy do not apply there.
"""

import json

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _host_settings,
    _is_hpc,
    _policy_mode,
    record_host,
    execute_remote_bash,
    probe_host,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"

BASE_SETTINGS = {
    "derecho": {"center": "ncar", "role": "login"},
    "laptop": {"hpc": False},
}

# An ordinary ~/.ssh/config, carrying the comment syntax older versions used.
# Nothing here reads it; the never-touches tests prove nothing writes it either.
SSH_CONFIG = """\
Host derecho
    HostName derecho.hpc.ucar.edu
    User someone
    ControlMaster auto
    # hpc-mcp: center=curc role=compute

Host newbox
    HostName newbox.example.edu
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty store, for the tests that watch record_host write one."""
    path = tmp_path / "store" / "hosts.json"
    path.parent.mkdir()
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
    ssh_hpc_server._STORE_CACHE = None
    ssh_hpc_server._ONBOARDING_SEEN.clear()
    yield path
    ssh_hpc_server._STORE_CACHE = None
    ssh_hpc_server._ONBOARDING_SEEN.clear()


@pytest.fixture
def recorded(store):
    """derecho and laptop already recorded; newbox and plainer are unknown."""
    store.write_text(json.dumps({"hosts": BASE_SETTINGS}))
    ssh_hpc_server._STORE_CACHE = None
    yield store


@pytest.fixture
def ssh_config(tmp_path, monkeypatch):
    """A real ~/.ssh/config that must come back byte for byte unchanged."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    path = home / ".ssh" / "config"
    path.write_text(SSH_CONFIG)
    monkeypatch.setenv("HOME", str(home))
    yield path


# ---------------------------------------------------------------------------
# hpc=false
# ---------------------------------------------------------------------------

class TestHpcFalse:
    def test_recorded_host_is_hpc_by_default(self, recorded):
        assert _is_hpc("derecho") is True
        assert _is_hpc("newbox") is True

    @pytest.mark.parametrize("value", ["false", "False", "no", "0", "off", False])
    def test_recognised_negatives(self, store, value):
        store.write_text(json.dumps({"hosts": {"box": {"hpc": value}}}))
        ssh_hpc_server._STORE_CACHE = None
        assert _is_hpc("box") is False

    def test_non_hpc_host_lifts_the_policy(self, recorded, mock_subprocess):
        assert _policy_mode("laptop") == "off"
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="laptop", command="sudo make install")
        mock_subprocess.assert_called_once()

    def test_non_hpc_host_still_runs_heavy_work_without_a_flag(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="laptop", command="python train.py")
        mock_subprocess.assert_called_once()

    def test_explicit_policy_beats_the_hpc_flag(self, store, mock_subprocess):
        store.write_text(json.dumps({"hosts": {"box": {"hpc": False, "policy": "strict"}}}))
        ssh_hpc_server._STORE_CACHE = None
        assert _policy_mode("box") == "strict"
        assert "Blocked" in execute_remote_bash(host="box", command="sudo ls")

    def test_hpc_host_is_still_guarded(self, recorded, mock_subprocess):
        assert _policy_mode("derecho") == "strict"
        assert "Blocked" in execute_remote_bash(host="derecho", command="sudo ls")

    def test_workstation_role_no_longer_exists(self):
        assert "workstation" not in ssh_hpc_server.VALID_ROLES


# ---------------------------------------------------------------------------
# First-contact notice
# ---------------------------------------------------------------------------

class TestOnboardingNotice:
    def test_unrecorded_host_gets_a_notice(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        result = execute_remote_bash(host="newbox", command="echo hi")
        assert "hi" in result
        assert "probe_host" in result
        assert "newbox" in result

    def test_notice_appears_only_once_per_host(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        first = execute_remote_bash(host="newbox", command="echo hi")
        second = execute_remote_bash(host="newbox", command="echo hi")
        assert "probe_host" in first
        assert "probe_host" not in second

    def test_recorded_host_gets_no_notice(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        assert "probe_host" not in execute_remote_bash(host="derecho", command="echo hi")

    def test_notice_does_not_hide_the_output(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="payload\n")
        result = execute_remote_bash(host="newbox", command="echo payload")
        assert result.startswith("payload")

    def test_notice_still_appears_on_a_failed_command(self, recorded, mock_subprocess):
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

    def test_reports_what_it_found(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        result = probe_host("newbox")
        assert "derecho1.hpc.ucar.edu" in result
        assert "PBS" in result
        assert "UABC0001" in result

    def test_infers_ncar_from_glade(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "center=ncar" in probe_host("newbox")

    def test_infers_curc_from_alpine_scratch(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0,
            stdout=self._probe_output(scheduler="sbatch", filesystems="/scratch/alpine /pl/active", account=""),
        )
        result = probe_host("newbox")
        assert "center=curc" in result
        assert "Slurm" in result

    def test_no_scheduler_suggests_not_hpc(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout=self._probe_output(scheduler="", filesystems="", account="", globus="no"),
        )
        result = probe_host("newbox")
        assert "hpc=false" in result
        assert "no scheduler" in result.lower()

    def test_asks_the_user_before_writing(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        result = probe_host("newbox")
        assert "record_host" in result
        assert "?" in result  # it poses questions for the user

    def test_probe_itself_emits_no_onboarding_notice(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "probe_host(" not in probe_host("newbox")

    def test_ssh_failure_is_reported(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Permission denied (publickey,keyboard-interactive).",
        )
        result = probe_host("newbox")
        assert "ssh -fN newbox" in result

    def test_already_recorded_host_says_so(self, recorded, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=self._probe_output())
        assert "already has settings recorded" in probe_host("derecho").lower()


# ---------------------------------------------------------------------------
# record_host
# ---------------------------------------------------------------------------

class TestRecordHostNeverTouchesSshConfig:
    """~/.ssh/config controls access to every host the user has. This server
    neither writes it nor reads it: a mangled line, a replaced symlink or a
    downgraded file mode would cost the user far more than the convenience is
    worth. Settings go to a small file this server owns instead, which can be
    deleted at any time with no consequence but lost defaults."""

    def test_ssh_config_is_left_byte_for_byte_identical(self, ssh_config, store):
        before = ssh_config.read_bytes()
        record_host("newbox", center="ncar", role="login", account="UABC0001")
        assert ssh_config.read_bytes() == before

    def test_no_backup_or_temp_file_is_left_beside_the_ssh_config(self, ssh_config, store):
        record_host("newbox", center="ncar")
        derived = [p.name for p in ssh_config.parent.iterdir()
                   if p.is_file() and p.name != "config"]
        assert derived == []

    def test_a_symlinked_ssh_config_survives(self, tmp_path, monkeypatch, store):
        real = tmp_path / "dotfiles" / "ssh_config"
        real.parent.mkdir()
        real.write_text(SSH_CONFIG)
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        link = home / ".ssh" / "config"
        link.symlink_to(real)
        monkeypatch.setenv("HOME", str(home))
        record_host("newbox", center="ncar")
        assert link.is_symlink()
        assert real.read_text() == SSH_CONFIG

    def test_ssh_config_permissions_are_untouched(self, ssh_config, store):
        ssh_config.chmod(0o600)
        record_host("newbox", center="ncar")
        assert oct(ssh_config.stat().st_mode)[-3:] == "600"

    def test_the_ssh_config_is_not_even_opened(self, ssh_config, store, monkeypatch):
        """Reading it is gone too, so an unreadable config must not matter."""
        ssh_config.chmod(0o000)
        try:
            assert _host_settings("derecho") == {}
            record_host("derecho", center="ncar")
            assert _host_settings("derecho")["center"] == "ncar"
        finally:
            ssh_config.chmod(0o600)


class TestRecordHostStore:
    def test_writes_the_settings_to_the_store(self, store):
        result = record_host("newbox", center="ncar", role="login", account="UABC0001")
        assert "newbox" in result
        assert str(store) in result
        assert store.exists()
        assert _host_settings("newbox") == {
            "center": "ncar", "role": "login", "account": "UABC0001",
        }

    def test_the_server_reads_it_back_immediately(self, store):
        record_host("newbox", center="curc", role="login")
        assert _host_settings("newbox")["center"] == "curc"

    def test_store_is_created_with_parent_directories(self, store, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "deeper" / "hosts.json"
        monkeypatch.setenv("HPC_SSH_MCP_STORE", str(target))
        ssh_hpc_server._STORE_CACHE = None
        record_host("newbox", center="ncar")
        assert target.exists()

    def test_store_is_private(self, store):
        record_host("newbox", center="ncar")
        assert oct(store.stat().st_mode)[-3:] == "600"

    def test_store_explains_itself(self, store):
        record_host("newbox", center="ncar")
        assert "delete" in store.read_text().lower()

    def test_re_annotating_updates_without_dropping_the_rest(self, store):
        record_host("newbox", center="ncar", account="OLD001")
        record_host("newbox", account="NEW002")
        settings = _host_settings("newbox")
        assert settings["account"] == "NEW002"
        assert settings["center"] == "ncar"
        assert "OLD001" not in store.read_text()

    def test_other_entries_survive(self, store):
        record_host("newbox", center="ncar")
        record_host("plainer", center="curc")
        assert _host_settings("newbox")["center"] == "ncar"
        assert _host_settings("plainer")["center"] == "curc"

    def test_deleting_the_store_restores_defaults(self, store):
        record_host("newbox", center="curc")
        assert _host_settings("newbox")["center"] == "curc"
        store.unlink()
        ssh_hpc_server._STORE_CACHE = None
        assert _host_settings("newbox") == {}

    def test_a_corrupt_store_is_ignored_not_fatal(self, store):
        store.write_text("this is not: a valid = anything\n\x00garbage\n")
        ssh_hpc_server._STORE_CACHE = None
        assert _host_settings("newbox") == {}

    def test_hpc_false_is_written(self, store):
        record_host("newbox", is_hpc=False)
        assert _is_hpc("newbox") is False

    def test_hpc_false_drops_hpc_only_keys(self, store):
        record_host("newbox", is_hpc=False, center="ncar", account="X1")
        assert set(_host_settings("newbox")) == {"hpc"}


class TestRecordHostValidation:
    def test_any_alias_can_be_recorded(self, store):
        """Nothing validates the alias against ~/.ssh/config any more: a typo
        surfaces on the next call as ssh's own "Could not resolve hostname"."""
        result = record_host("not-in-ssh-config", center="ncar")
        assert "not-in-ssh-config" in result
        assert _host_settings("not-in-ssh-config")["center"] == "ncar"

    def test_will_not_take_a_wildcard(self, store):
        result = record_host("*", center="ncar")
        assert "wildcard" in result.lower() or "not a specific host" in result.lower()
        assert not store.exists() or "*" not in store.read_text()

    @pytest.mark.parametrize("kwargs,bad", [
        ({"center": "nersc"}, "center"),
        ({"role": "wizard"}, "role"),
        ({"policy": "yolo"}, "policy"),
        ({"account": "a; rm -rf /"}, "account"),
        ({"globus": "not-a-uuid"}, "globus"),
        ({"scratch": "/x\npolicy=off"}, "scratch"),
    ])
    def test_rejects_bad_values(self, store, kwargs, bad):
        with pytest.raises(ValueError, match=bad):
            record_host("newbox", **kwargs)
        assert not store.exists()

    def test_nothing_to_write_is_refused(self, store):
        result = record_host("newbox")
        assert "nothing" in result.lower()
        assert not store.exists()

    def test_clears_the_onboarding_notice(self, store, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="hi\n")
        record_host("newbox", center="ncar", role="login")
        assert "probe_host" not in execute_remote_bash(host="newbox", command="echo hi")


class TestRecordHostToolAnnotations:
    @pytest.mark.asyncio
    async def test_tool_annotations(self):
        from fastmcp import Client
        async with Client(ssh_hpc_server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["probe_host"].annotations.readOnlyHint is True
        assert tools["record_host"].annotations.readOnlyHint is False
