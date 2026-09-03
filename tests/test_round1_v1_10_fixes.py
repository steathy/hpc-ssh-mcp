"""Regression tests for the round 1 review of 1.10.0 (a local review note).

Each class pins one finding. As in the previous round, most of these were
invisible to the mocked suite: they lived in the store's caching, in a message
attached at the wrong layer, in text the server itself suggests, or in what a
build produces. Where the remote matters, tests/test_integration.py has the
live counterpart.
"""

import json
import subprocess

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server


# ---------------------------------------------------------------------------
# F3: a hand-edit of the settings file must be seen by the running server
# ---------------------------------------------------------------------------
# Every refusal tells the user to edit hosts.json and retry. The store was read
# once into a module cache that only record_host cleared, so the edit the
# refusal asked for had no effect until a restart the message never mentioned.

@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "hosts.json"
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    return path


class TestTheStoreIsReadNotCached:
    def test_a_policy_added_by_hand_applies_to_the_next_call(self, store):
        assert ssh_hpc_server._policy_mode("cluster") == "strict"      # primes any cache
        store.write_text(json.dumps({"hosts": {"cluster": {"policy": "off"}}}))
        assert ssh_hpc_server._policy_mode("cluster") == "off"

    def test_the_refusal_stops_once_the_user_has_done_what_it_said(self, store, mock_subprocess):
        first = ssh_hpc_server.execute_remote_bash(host="cluster", command="sudo ls")
        assert "Blocked by policy" in first
        store.write_text(json.dumps({"hosts": {"cluster": {"policy": "off"}}}))
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        assert "Blocked" not in ssh_hpc_server.execute_remote_bash(host="cluster", command="sudo ls")

    def test_a_globus_uuid_added_by_hand_resolves(self, store):
        uuid = "d33b3614-6d04-11e5-ba46-22000b92c6ec"
        with pytest.raises(ValueError):
            ssh_hpc_server._resolve_collection("derecho")
        store.write_text(json.dumps({"hosts": {"derecho": {"globus": uuid}}}))
        assert ssh_hpc_server._resolve_collection("derecho") == uuid

    def test_there_is_no_store_cache_to_go_stale(self):
        assert not hasattr(ssh_hpc_server, "_STORE_CACHE")
