"""Globus transfer tools.

The Globus CLI talks to the Globus API, not to a cluster, so these tools run
it locally: no login node is touched and no SSH session is needed. The CLI
owns the OAuth tokens (`globus login` once, in the user's own terminal), the
same out-of-band pattern as `ssh -fN <host>` for Duo.

Exit code 4 from the CLI means "authentication or authorization requirement
not met" — a missing login, or a mapped collection whose data_access consent
has not been granted. Those are the failures worth translating into an exact
command the user can run.
"""

import json

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _globus_env,
    _resolve_collection,
    globus_find_collection,
    globus_ls,
    globus_status,
    globus_task_cancel,
    globus_task_status,
    globus_transfer,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"
ALPINE = "aa3b3614-6d04-11e5-ba46-22000b92c6ff"

PROFILE_TOML = f"""
[globus.collections]
glade = "{GLADE}"
alpine = "{ALPINE}"
"""


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    path = tmp_path / "hosts.toml"
    path.write_text(PROFILE_TOML)
    monkeypatch.setenv("HPC_SSH_MCP_CONFIG", str(path))
    ssh_hpc_server._PROFILE_CACHE = None
    yield path
    ssh_hpc_server._PROFILE_CACHE = None


@pytest.fixture
def have_cli(monkeypatch):
    monkeypatch.setattr(ssh_hpc_server, "_globus_cli_available", lambda: True)


def argv(mock):
    return mock.call_args[0][0]


def json_result(payload, returncode=0):
    return make_completed_process(returncode=returncode, stdout=json.dumps(payload))


# ---------------------------------------------------------------------------
# Environment and availability
# ---------------------------------------------------------------------------

class TestCliEnvironment:
    def test_interactive_is_disabled(self):
        assert _globus_env()["GLOBUS_CLI_INTERACTIVE"] == "0"

    def test_inherits_the_parent_environment(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/someone")
        assert _globus_env()["HOME"] == "/home/someone"

    def test_missing_cli_is_reported_with_install_and_login_steps(self, monkeypatch, mock_subprocess):
        monkeypatch.setattr(ssh_hpc_server, "_globus_cli_available", lambda: False)
        result = globus_status()
        assert "globus login" in result
        assert "not installed" in result.lower() or "not found" in result.lower()
        mock_subprocess.assert_not_called()


# ---------------------------------------------------------------------------
# Collection aliases
# ---------------------------------------------------------------------------

class TestCollectionResolution:
    def test_alias_resolves_to_uuid(self, profiles):
        assert _resolve_collection("glade") == GLADE

    def test_uuid_passes_through(self, profiles):
        assert _resolve_collection(GLADE) == GLADE

    def test_uppercase_uuid_is_normalised(self, profiles):
        assert _resolve_collection(GLADE.upper()) == GLADE

    def test_unknown_alias_is_rejected_with_the_known_ones(self, profiles):
        with pytest.raises(ValueError) as exc:
            _resolve_collection("campaign")
        assert "glade" in str(exc.value)
        assert "globus_find_collection" in str(exc.value)

    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "../etc", "abc; rm -rf /", "-x"])
    def test_malformed_values_are_rejected(self, profiles, bad):
        with pytest.raises(ValueError):
            _resolve_collection(bad)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

class TestGlobusStatus:
    def test_reports_identity(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result({"username": "me@ucar.edu", "name": "Me"})
        result = globus_status()
        assert "me@ucar.edu" in result
        assert argv(mock_subprocess)[:2] == ["globus", "whoami"]

    def test_logged_out_explains_how_to_log_in(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=4, stderr="MissingLoginError: Missing login for auth.globus.org\n",
        )
        result = globus_status()
        assert "globus login" in result
        assert "your own terminal" in result.lower()


class TestGlobusFindCollection:
    def test_searches_and_lists_uuids(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result([
            {"id": GLADE, "display_name": "NCAR GLADE", "owner_string": "ncar@globus.org"},
        ])
        result = globus_find_collection("NCAR GLADE")
        assert GLADE in result
        assert "NCAR GLADE" in result
        cmd = argv(mock_subprocess)
        assert cmd[:3] == ["globus", "endpoint", "search"]
        assert "NCAR GLADE" in cmd
        assert "--format" in cmd and "json" in cmd

    def test_no_results_says_so(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result([])
        assert "no collection" in globus_find_collection("nothing").lower()

    def test_search_text_is_an_argv_entry_not_a_shell_string(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result([])
        globus_find_collection("weird; rm -rf /")
        assert "weird; rm -rf /" in argv(mock_subprocess)


class TestGlobusLs:
    def test_lists_a_path(self, have_cli, profiles, mock_subprocess):
        mock_subprocess.return_value = json_result({"DATA": [
            {"name": "run1", "type": "dir", "size": 0},
            {"name": "out.nc", "type": "file", "size": 1234},
        ]})
        result = globus_ls("glade", "/glade/work/me")
        assert "run1" in result and "out.nc" in result
        cmd = argv(mock_subprocess)
        assert cmd[:2] == ["globus", "ls"]
        assert f"{GLADE}:/glade/work/me" in cmd

    def test_consent_required_names_the_exact_command(self, have_cli, profiles, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=4,
            stderr=f"ConsentRequired: Missing required data_access consent for {GLADE}\n",
        )
        result = globus_ls("glade", "/glade/work/me")
        assert "globus session consent" in result
        assert f"{GLADE}/data_access" in result
        assert "transfer.api.globus.org" in result


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

class TestGlobusTransfer:
    def _submit(self, mock, **kwargs):
        mock.return_value = json_result({"task_id": "1234abcd-0000-0000-0000-00000000cafe",
                                         "message": "The transfer has been accepted"})
        return globus_transfer(
            source="glade", source_path="/glade/work/me/run1",
            dest="alpine", dest_path="/scratch/alpine/me/run1", **kwargs,
        )

    def test_submits_and_returns_the_task_id(self, have_cli, profiles, mock_subprocess):
        result = self._submit(mock_subprocess)
        assert "1234abcd-0000-0000-0000-00000000cafe" in result
        assert "globus_task_status" in result
        cmd = argv(mock_subprocess)
        assert cmd[:2] == ["globus", "transfer"]
        assert f"{GLADE}:/glade/work/me/run1" in cmd
        assert f"{ALPINE}:/scratch/alpine/me/run1" in cmd

    def test_defaults_are_idempotent_and_labelled(self, have_cli, profiles, mock_subprocess):
        cmd = " ".join(argv(mock_subprocess)) if self._submit(mock_subprocess) else ""
        assert "--sync-level mtime" in cmd
        assert "--label" in cmd

    def test_recursive_flag(self, have_cli, profiles, mock_subprocess):
        self._submit(mock_subprocess, recursive=True)
        assert "--recursive" in argv(mock_subprocess)

    def test_dry_run_flag(self, have_cli, profiles, mock_subprocess):
        self._submit(mock_subprocess, dry_run=True)
        assert "--dry-run" in argv(mock_subprocess)

    @pytest.mark.parametrize("level", ["exists", "size", "mtime", "checksum"])
    def test_valid_sync_levels(self, have_cli, profiles, mock_subprocess, level):
        self._submit(mock_subprocess, sync_level=level)
        assert level in argv(mock_subprocess)

    def test_invalid_sync_level_is_rejected(self, have_cli, profiles):
        with pytest.raises(ValueError, match="sync_level"):
            globus_transfer(source="glade", source_path="/a", dest="alpine", dest_path="/b",
                            sync_level="whatever")

    def test_delete_extra_needs_confirmation(self, have_cli, profiles, mock_subprocess):
        result = globus_transfer(
            source="glade", source_path="/a", dest="alpine", dest_path="/b",
            delete_destination_extra=True,
        )
        assert "confirm_destructive" in result
        mock_subprocess.assert_not_called()

    def test_delete_extra_runs_with_confirmation(self, have_cli, profiles, mock_subprocess):
        mock_subprocess.return_value = json_result({"task_id": "t"})
        globus_transfer(
            source="glade", source_path="/a", dest="alpine", dest_path="/b",
            delete_destination_extra=True, confirm_destructive=True,
        )
        assert "--delete-destination-extra" in argv(mock_subprocess)

    def test_label_is_validated(self, have_cli, profiles):
        with pytest.raises(ValueError, match="label"):
            globus_transfer(source="glade", source_path="/a", dest="alpine", dest_path="/b",
                            label="bad\nlabel")

    def test_same_collection_and_path_is_refused(self, have_cli, profiles, mock_subprocess):
        result = globus_transfer(source="glade", source_path="/a", dest="glade", dest_path="/a")
        assert "same" in result.lower()
        mock_subprocess.assert_not_called()


class TestGlobusTaskStatus:
    TASK = "1234abcd-0000-0000-0000-00000000cafe"

    def test_summarises_a_succeeded_task(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result({
            "status": "SUCCEEDED", "files_transferred": 12, "bytes_transferred": 4096,
            "label": "run1", "source_endpoint_display_name": "NCAR GLADE",
        })
        result = globus_task_status(self.TASK)
        assert "SUCCEEDED" in result
        assert "12" in result
        assert argv(mock_subprocess)[:3] == ["globus", "task", "show"]

    def test_failed_task_also_fetches_the_last_error_event(self, have_cli, mock_subprocess):
        mock_subprocess.side_effect = [
            json_result({"status": "FAILED", "files_transferred": 0, "bytes_transferred": 0}),
            json_result([{"code": "PERMISSION_DENIED", "description": "permission denied",
                          "details": "/scratch/alpine/me: permission denied"}]),
        ]
        result = globus_task_status(self.TASK)
        assert "FAILED" in result
        assert "PERMISSION_DENIED" in result
        assert mock_subprocess.call_count == 2
        assert argv(mock_subprocess)[:3] == ["globus", "task", "event-list"]

    def test_active_task_does_not_fetch_events(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result({"status": "ACTIVE", "files_transferred": 3,
                                                    "bytes_transferred": 100})
        globus_task_status(self.TASK)
        assert mock_subprocess.call_count == 1

    @pytest.mark.parametrize("bad", ["", "not-a-uuid", "abc; rm -rf /", "-x"])
    def test_rejects_malformed_task_ids(self, have_cli, bad):
        with pytest.raises(ValueError, match="task ID"):
            globus_task_status(bad)


class TestGlobusTaskCancel:
    TASK = "1234abcd-0000-0000-0000-00000000cafe"

    def test_cancels(self, have_cli, mock_subprocess):
        mock_subprocess.return_value = json_result({"code": "Canceled", "message": "The task has been cancelled"})
        result = globus_task_cancel(self.TASK)
        assert "cancel" in result.lower()
        assert argv(mock_subprocess)[:3] == ["globus", "task", "cancel"]

    def test_rejects_malformed_task_id(self, have_cli):
        with pytest.raises(ValueError, match="task ID"):
            globus_task_cancel("nope")


# ---------------------------------------------------------------------------
# Annotations: the Globus tools must be classified like the SSH ones
# ---------------------------------------------------------------------------

class TestGlobusAnnotations:
    @pytest.mark.asyncio
    async def test_read_only_and_mutating_tools_are_marked(self):
        from fastmcp import Client
        async with Client(ssh_hpc_server.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
        for name in ("globus_status", "globus_find_collection", "globus_ls", "globus_task_status"):
            assert tools[name].annotations.readOnlyHint is True, name
        for name in ("globus_transfer", "globus_task_cancel"):
            assert tools[name].annotations.readOnlyHint is False, name
        assert tools["globus_task_cancel"].annotations.destructiveHint is True
