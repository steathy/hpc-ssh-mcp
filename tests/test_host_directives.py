"""Host metadata read from ~/.ssh/config, not a second config file.

Every host this server talks to is already described in ~/.ssh/config. A
separate TOML file meant listing every alias twice and keeping the two in
sync. Instead an optional comment inside the Host block carries what SSH
itself has no keyword for:

    Host derecho
        HostName derecho.hpc.ucar.edu
        ControlMaster auto
        # hpc-mcp: center=ncar role=login account=UABC0001

Nothing is required. With no annotation at all the server probes for the
scheduler and treats the host as a login node, which is the safe default.
"""

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _host_directives,
    _host_role,
    _policy_mode,
    _resolve_collection,
    execute_remote_bash,
    list_queue,
    run_on_compute,
    submit_job,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"

SSH_CONFIG = f"""
# A normal SSH config with hpc-mcp annotations mixed in.

Host derecho
    HostName derecho.hpc.ucar.edu
    User someone
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    # hpc-mcp: center=ncar role=login account=UABC0001 scratch=/glade/derecho/scratch/$USER
    # hpc-mcp: globus={GLADE}
    ServerAliveInterval 60

Host casper
    HostName casper.hpc.ucar.edu
    #hpc-mcp:center=ncar role=login
    # an ordinary comment that should be ignored

Host ncar-data
    HostName data-access.ucar.edu
    # hpc-mcp: center=ncar role=data-access

Host cu-alpine
    HostName login.rc.colorado.edu
    # HPC-MCP: center=curc role=login account=ucb-general

Host my-box
    HostName 10.0.0.5
    # hpc-mcp: role=workstation policy=off

Host plain-host
    HostName plain.example.edu

Host *
    ServerAliveInterval 30
"""


@pytest.fixture
def ssh_config(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text(SSH_CONFIG)
    monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(path))
    monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
    ssh_hpc_server._DIRECTIVE_CACHE = None
    yield path
    ssh_hpc_server._DIRECTIVE_CACHE = None


class TestParsing:
    def test_reads_every_key(self, ssh_config):
        d = _host_directives("derecho")
        assert d["center"] == "ncar"
        assert d["role"] == "login"
        assert d["account"] == "UABC0001"
        assert d["scratch"] == "/glade/derecho/scratch/$USER"
        assert d["globus"] == GLADE

    def test_multiple_annotation_lines_merge(self, ssh_config):
        assert {"center", "globus"} <= set(_host_directives("derecho"))

    def test_tolerates_missing_spaces_and_any_case(self, ssh_config):
        assert _host_directives("casper")["center"] == "ncar"
        assert _host_directives("cu-alpine")["center"] == "curc"

    def test_ordinary_comments_are_ignored(self, ssh_config):
        assert set(_host_directives("casper")) == {"center", "role"}

    def test_host_without_annotations_has_none(self, ssh_config):
        assert _host_directives("plain-host") == {}

    def test_host_not_in_the_file_at_all(self, ssh_config):
        assert _host_directives("never-heard-of-it") == {}

    def test_missing_ssh_config_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(tmp_path / "absent"))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("derecho") == {}

    def test_unreadable_config_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(tmp_path))  # a directory
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("derecho") == {}

    def test_malformed_annotation_does_not_break_the_rest(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host a\n    # hpc-mcp: this-has-no-equals center=ncar =novalue novalue=\n"
        )
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("a")["center"] == "ncar"


class TestPatternMatching:
    def test_wildcard_block_applies_to_every_host(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host derecho\n    # hpc-mcp: center=ncar\n\n"
            "Host *\n    # hpc-mcp: policy=permissive\n"
        )
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("derecho")["center"] == "ncar"
        assert _host_directives("derecho")["policy"] == "permissive"
        assert _host_directives("anything-else")["policy"] == "permissive"

    def test_first_match_wins_like_ssh(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.write_text(
            "Host derecho\n    # hpc-mcp: role=login\n\n"
            "Host *\n    # hpc-mcp: role=workstation\n"
        )
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("derecho")["role"] == "login"

    def test_several_aliases_on_one_host_line(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.write_text("Host derecho derecho2 dc\n    # hpc-mcp: center=ncar\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        for alias in ("derecho", "derecho2", "dc"):
            assert _host_directives(alias)["center"] == "ncar", alias

    def test_glob_patterns_match(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config"
        cfg.write_text("Host ncar-*\n    # hpc-mcp: center=ncar\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("ncar-data")["center"] == "ncar"
        assert _host_directives("cu-alpine") == {}

    def test_include_directive_is_followed(self, tmp_path, monkeypatch):
        (tmp_path / "extra").write_text("Host derecho\n    # hpc-mcp: center=ncar\n")
        cfg = tmp_path / "config"
        cfg.write_text(f"Include {tmp_path / 'extra'}\n\nHost other\n    HostName x\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        assert _host_directives("derecho")["center"] == "ncar"


class TestDirectivesDriveBehaviour:
    def test_role(self, ssh_config):
        assert _host_role("derecho") == "login"
        assert _host_role("ncar-data") == "dtn"
        assert _host_role("my-box") == "workstation"
        assert _host_role("plain-host") == "login"

    def test_center_picks_the_scheduler_without_probing(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        list_queue(host="derecho")
        assert mock_subprocess.call_count == 1
        assert "qstat" in mock_subprocess.call_args.kwargs["input"]

        mock_subprocess.reset_mock()
        list_queue(host="cu-alpine")
        assert "squeue" in mock_subprocess.call_args.kwargs["input"]

    def test_unannotated_host_still_probes(self, ssh_config, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="sbatch\n"),
            make_completed_process(returncode=0, stdout=""),
        ]
        list_queue(host="plain-host")
        assert mock_subprocess.call_count == 2

    def test_account_defaults_from_the_annotation(self, ssh_config, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        run_on_compute(host="derecho", command="true")
        assert "-A UABC0001" in mock_subprocess.call_args.kwargs["input"]

    def test_scratch_is_suggested_by_submit_job(self, ssh_config, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        result = submit_job(host="derecho", job_script_content="#!/bin/bash")
        assert "/glade/derecho/scratch/$USER" in result

    def test_policy_mode(self, ssh_config, mock_subprocess):
        assert _policy_mode("derecho") == "strict"
        assert _policy_mode("my-box") == "off"
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="my-box", command="sudo ls")
        mock_subprocess.assert_called_once()

    def test_environment_variable_still_overrides_policy(self, ssh_config, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_POLICY", "off")
        assert _policy_mode("derecho") == "off"

    def test_globus_collection_by_host_alias(self, ssh_config):
        assert _resolve_collection("derecho") == GLADE

    def test_globus_uuid_still_works(self, ssh_config):
        assert _resolve_collection(GLADE) == GLADE

    def test_unknown_globus_name_lists_the_annotated_hosts(self, ssh_config):
        with pytest.raises(ValueError) as exc:
            _resolve_collection("casper")
        assert "derecho" in str(exc.value)
        assert "globus_find_collection" in str(exc.value)


class TestNoTomlAnymore:
    def test_module_does_not_import_tomllib(self):
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        assert "tomllib" not in source
        assert "hosts.toml" not in source

    def test_example_toml_file_is_gone(self):
        import pathlib
        root = pathlib.Path(ssh_hpc_server.__file__).parent
        assert not (root / "hosts.example.toml").exists()
