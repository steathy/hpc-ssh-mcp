"""The settings file this server writes is JSON.

It is machine-written and machine-read, so it uses a format with a parser
that already exists rather than a line syntax invented here. `~/.ssh/config`
stays the place for hand-written annotations, and still wins over this.
"""

import json

import pytest

import ssh_hpc_server
from ssh_hpc_server import (
    _globus_collections,
    _host_directives,
    _is_hpc,
    _load_store,
    _resolve_collection,
    annotate_host,
)

GLADE = "d33b3614-6d04-11e5-ba46-22000b92c6ec"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "hosts.json"
    monkeypatch.setenv("HPC_SSH_MCP_STORE", str(path))
    monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(tmp_path / "no-such-ssh-config"))
    ssh_hpc_server._DIRECTIVE_CACHE = None
    yield path
    ssh_hpc_server._DIRECTIVE_CACHE = None


def payload(store):
    return json.loads(store.read_text())


class TestFormat:
    def test_file_is_valid_json(self, store):
        annotate_host("derecho", center="ncar", role="login")
        payload(store)  # raises if it is not

    def test_default_filename_is_json(self):
        assert ssh_hpc_server.DEFAULT_STORE.endswith(".json")

    def test_shape_is_hosts_keyed_by_alias(self, store):
        annotate_host("derecho", center="ncar", role="login", account="UABC0001")
        data = payload(store)
        assert data["hosts"]["derecho"] == {
            "center": "ncar", "role": "login", "account": "UABC0001",
        }

    def test_carries_a_note_for_whoever_opens_it(self, store):
        annotate_host("derecho", center="ncar")
        note = " ".join(str(v) for k, v in payload(store).items() if k != "hosts")
        assert "hpc-ssh-mcp" in note
        assert "delete" in note.lower()

    def test_hpc_is_a_real_boolean_not_a_string(self, store):
        annotate_host("laptop", is_hpc=False)
        assert payload(store)["hosts"]["laptop"]["hpc"] is False

    def test_written_readably(self, store):
        annotate_host("derecho", center="ncar")
        assert store.read_text().count("\n") > 2  # indented, not one long line

    def test_file_is_private(self, store):
        annotate_host("derecho", center="ncar")
        assert oct(store.stat().st_mode)[-3:] == "600"


class TestRoundTrip:
    def test_values_come_back(self, store):
        annotate_host("derecho", center="ncar", role="login", account="UABC0001",
                      scratch="/glade/derecho/scratch/$USER", globus=GLADE)
        d = _host_directives("derecho")
        assert d["center"] == "ncar"
        assert d["role"] == "login"
        assert d["account"] == "UABC0001"
        assert d["scratch"] == "/glade/derecho/scratch/$USER"
        assert d["globus"] == GLADE

    def test_hpc_false_round_trips(self, store):
        annotate_host("laptop", is_hpc=False)
        assert _is_hpc("laptop") is False

    def test_second_host_does_not_clobber_the_first(self, store):
        annotate_host("derecho", center="ncar")
        annotate_host("cu-alpine", center="curc")
        hosts = payload(store)["hosts"]
        assert hosts["derecho"]["center"] == "ncar"
        assert hosts["cu-alpine"]["center"] == "curc"

    def test_rewriting_a_host_updates_the_keys_it_is_given(self, store):
        annotate_host("derecho", center="ncar", account="OLD001")
        annotate_host("derecho", account="NEW002")
        entry = payload(store)["hosts"]["derecho"]
        assert entry["account"] == "NEW002"
        assert entry["center"] == "ncar"  # not passed this time, so not dropped

    def test_globus_uuid_from_the_store_resolves_as_an_alias(self, store):
        """A UUID written by annotate_host must be usable, not write-only."""
        annotate_host("derecho", center="ncar", globus=GLADE)
        assert _resolve_collection("derecho") == GLADE
        assert _globus_collections()["derecho"] == GLADE


class TestBadInput:
    def _write(self, store, text):
        store.write_text(text)
        ssh_hpc_server._DIRECTIVE_CACHE = None

    def test_missing_file(self, store):
        assert _load_store() == {}

    def test_not_json(self, store):
        self._write(store, "derecho: center=ncar\n")  # the old line format
        assert _load_store() == {}

    def test_truncated_json(self, store):
        self._write(store, '{"hosts": {"derecho": {"center": "ncar"')
        assert _load_store() == {}

    def test_top_level_is_not_an_object(self, store):
        self._write(store, '["derecho"]')
        assert _load_store() == {}

    def test_hosts_is_not_an_object(self, store):
        self._write(store, '{"hosts": "derecho"}')
        assert _load_store() == {}

    def test_an_entry_that_is_not_an_object_is_skipped(self, store):
        self._write(store, '{"hosts": {"derecho": "ncar", "casper": {"center": "ncar"}}}')
        loaded = _load_store()
        assert "derecho" not in loaded
        assert loaded["casper"]["center"] == "ncar"

    def test_a_bad_host_key_is_skipped(self, store):
        self._write(store, '{"hosts": {"bad; host": {"center": "ncar"}, "ok": {"center": "curc"}}}')
        loaded = _load_store()
        assert "bad; host" not in loaded
        assert loaded["ok"]["center"] == "curc"

    def test_nested_values_are_skipped_not_fatal(self, store):
        self._write(store, '{"hosts": {"derecho": {"center": {"nested": 1}, "role": "login"}}}')
        loaded = _load_store()
        assert loaded["derecho"] == {"role": "login"}

    def test_a_corrupt_file_is_not_silently_overwritten(self, store):
        """Rewriting must not discard entries we simply failed to read."""
        self._write(store, "not json at all")
        result = annotate_host("derecho", center="ncar")
        assert "could not" in result.lower() or "unreadable" in result.lower()
        assert store.read_text() == "not json at all"


class TestPrecedence:
    def test_ssh_config_still_wins(self, tmp_path, monkeypatch, store):
        cfg = tmp_path / "config"
        cfg.write_text("Host derecho\n    # hpc-mcp: center=ncar\n")
        monkeypatch.setenv("HPC_SSH_MCP_SSH_CONFIG", str(cfg))
        ssh_hpc_server._DIRECTIVE_CACHE = None
        annotate_host("derecho", center="curc", role="login")
        assert _host_directives("derecho")["center"] == "ncar"   # the user's own word
        assert _host_directives("derecho")["role"] == "login"    # the store fills the gap
