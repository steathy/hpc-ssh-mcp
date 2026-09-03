"""Host settings come from the store this server owns, and nowhere else.

~/.ssh/config is not read at all. Earlier versions wrote a `# hpc-mcp:` comment
into it (1.5.0/1.6.0), then stopped writing but kept reading it (1.7.0/1.8.0).
Reading it was still wrong: it kept a parser for someone else's file format in
the tree, it gave "annotation" two meanings, and a `Host *` block silently
answered "yes, this host is described" for every alias that had never been
mentioned. Settings live in one place now.
"""

import json

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _host_role,
    _host_settings,
    _policy_mode,
    _resolve_collection,
    execute_remote_bash,
    list_queue,
    run_on_compute,
    submit_job,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"

SETTINGS = {
    "derecho": {
        "center": "ncar", "role": "login", "account": "UABC0001",
        "scratch": "/glade/derecho/scratch/$USER", "globus": GLADE,
    },
    "casper": {"center": "ncar", "role": "login"},
    "ncar-data": {"center": "ncar", "role": "dtn"},
    "cu-alpine": {"center": "curc", "role": "login", "account": "ucb-general"},
    "my-box": {"hpc": False},
}

SSH_CONFIG_WITH_ANNOTATIONS = f"""
Host derecho
    HostName derecho.hpc.ucar.edu
    # hpc-mcp: center=curc role=compute account=SHOULD-BE-IGNORED
    # hpc-mcp: globus=ffffffff-ffff-ffff-ffff-ffffffffffff

Host *
    # hpc-mcp: policy=off
    ServerAliveInterval 30

Host only-in-ssh-config
    HostName elsewhere.example.edu
    # hpc-mcp: center=ncar account={GLADE}
"""


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A populated store, and an ~/.ssh/config that must have no effect."""
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": SETTINGS}))
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    monkeypatch.delenv("HPC_SSH_MCP_POLICY", raising=False)

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(SSH_CONFIG_WITH_ANNOTATIONS)
    monkeypatch.setenv("HOME", str(home))

    yield path


class TestNothingReadsSshConfig:
    def test_an_annotated_ssh_config_has_no_effect(self, settings):
        """The fixture's ~/.ssh/config contradicts the store on every key."""
        recorded = _host_settings("derecho")
        assert recorded["center"] == "ncar"
        assert recorded["role"] == "login"
        assert recorded["account"] == "UABC0001"
        assert recorded["globus"] == GLADE

    def test_a_host_only_in_the_ssh_config_is_unknown(self, settings):
        assert _host_settings("only-in-ssh-config") == {}

    def test_a_wildcard_block_cannot_describe_every_host(self, settings):
        """`Host * / # hpc-mcp: policy=off` used to apply to every alias."""
        assert _host_settings("never-mentioned") == {}
        assert _policy_mode("never-mentioned") == "strict"

    def test_the_ssh_config_parser_is_gone(self):
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        for gone in (
            "hpc-mcp", "_parse_ssh_config", "_ssh_config_path", "_ssh_config_knows",
            "_ssh_config_annotations", "HPC_SSH_MCP_SSH_CONFIG", "_DIRECTIVE_CACHE",
        ):
            assert gone not in source, gone

    def test_no_pattern_matching_machinery_remains(self):
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        assert "fnmatch" not in source
        assert "import glob" not in source

    def test_a_host_alias_is_still_matched_exactly(self, settings):
        """No globbing: the store is keyed by the alias the user connects with."""
        assert _host_settings("derecho")
        assert _host_settings("derech") == {}
        assert _host_settings("derecho2") == {}


class TestSettingsDriveBehaviour:
    def test_role(self, settings):
        assert _host_role("derecho") == "login"
        assert _host_role("ncar-data") == "dtn"
        assert _host_role("unrecorded") == "login"

    def test_center_picks_the_scheduler_without_probing(self, settings, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        list_queue(host="derecho")
        assert mock_subprocess.call_count == 1
        assert "qstat" in mock_subprocess.call_args.kwargs["input"]

        mock_subprocess.reset_mock()
        list_queue(host="cu-alpine")
        assert "squeue" in mock_subprocess.call_args.kwargs["input"]

    def test_an_unrecorded_host_still_probes(self, settings, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="sbatch\n"),
            make_completed_process(returncode=0, stdout=""),
        ]
        list_queue(host="unrecorded")
        assert mock_subprocess.call_count == 2

    def test_account_defaults_from_the_store(self, settings, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        run_on_compute(host="derecho", command="true")
        assert "-A UABC0001" in mock_subprocess.call_args.kwargs["input"]

    def test_scratch_is_suggested_by_submit_job(self, settings, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="1.desched1\n"),
        ]
        result = submit_job(host="derecho", job_script_content="#!/bin/bash")
        assert "/glade/derecho/scratch/$USER" in result

    def test_policy_mode(self, settings, mock_subprocess):
        assert _policy_mode("derecho") == "strict"
        assert _policy_mode("my-box") == "off"  # hpc=false lifts the policy
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="my-box", command="sudo ls")
        mock_subprocess.assert_called_once()

    def test_environment_variable_still_overrides_policy(self, settings, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MCP_POLICY", "off")
        assert _policy_mode("derecho") == "off"

    def test_globus_collection_by_host_alias(self, settings):
        assert _resolve_collection("derecho") == GLADE

    def test_globus_uuid_still_works(self, settings):
        assert _resolve_collection(GLADE) == GLADE

    def test_unknown_globus_name_lists_the_recorded_hosts(self, settings):
        with pytest.raises(ValueError) as exc:
            _resolve_collection("casper")
        assert "derecho" in str(exc.value)
        assert "globus_find_collection" in str(exc.value)


class TestOneWordForOneThing:
    """"annotation" named both the ssh-config comment syntax and the store's
    contents, which is how the two blurred together. The store holds settings."""

    def test_the_tool_is_record_host(self):
        assert callable(ssh_hpc_server.record_host)
        assert not hasattr(ssh_hpc_server, "annotate_host")

    def test_the_old_identifiers_are_gone(self):
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        for gone in ("_ANNOTATION_KEYS", "_format_annotation", "_validate_annotation",
                     "annotate_host", "_host_directives"):
            assert gone not in source, gone

    def test_no_docstring_calls_a_setting_an_annotation(self):
        """Every docstring in this module is read by the model. MCP's own
        ToolAnnotations is a different thing entirely and keeps its name."""
        import inspect
        for name, obj in vars(ssh_hpc_server).items():
            if (inspect.isfunction(obj) and obj.__module__ == "ssh_hpc_server"
                    and obj.__doc__):
                assert "annotat" not in obj.__doc__.lower(), name

    def test_no_toml_anymore(self):
        import inspect
        source = inspect.getsource(ssh_hpc_server)
        assert "tomllib" not in source
        assert "hosts.toml" not in source
