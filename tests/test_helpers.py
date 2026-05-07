import pytest
import subprocess
from unittest.mock import patch

from ssh_hpc_server import _validate_host, _run_raw, _format_result, _run


class TestValidateHost:
    @pytest.mark.parametrize("host", [
        "derecho",
        "login.ncar.edu",
        "user@host",
        "my-cluster_01",
        "192.168.1.1",
    ])
    def test_accepts_valid_hosts(self, host):
        _validate_host(host)  # should not raise

    @pytest.mark.parametrize("host", [
        "",
        "host; rm -rf /",
        "host$(cmd)",
        "host | cat",
        "host\ninjection",
        "host`whoami`",
    ])
    def test_rejects_invalid_hosts(self, host):
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            _validate_host(host)


class TestRunRaw:
    def test_returns_success(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="hello\n", stderr="",
        )
        rc, out, err = _run_raw(["echo", "hello"])
        assert rc == 0
        assert out == "hello\n"
        assert err == ""

    def test_returns_nonzero_exit(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stdout="", stderr="not found\n",
        )
        rc, out, err = _run_raw(["false"])
        assert rc == 1
        assert err == "not found\n"

    def test_handles_timeout(self, mock_subprocess):
        mock_subprocess.side_effect = subprocess.TimeoutExpired(
            cmd=["ssh", "host", "sleep 999"], timeout=5,
        )
        rc, out, err = _run_raw(["ssh", "host", "sleep 999"], timeout=5)
        assert rc == -1
        assert "Timed out after 5s" in err

    def test_handles_missing_binary(self, mock_subprocess):
        mock_subprocess.side_effect = FileNotFoundError()
        rc, out, err = _run_raw(["nonexistent"])
        assert rc == -1
        assert "Command not found" in err

    def test_passes_input_data(self, mock_subprocess):
        from tests.conftest import make_completed_process
        mock_subprocess.return_value = make_completed_process(returncode=0)
        _run_raw(["cat"], input_data="hello world")
        mock_subprocess.assert_called_once()
        call_kwargs = mock_subprocess.call_args
        assert call_kwargs.kwargs.get("input") == "hello world" or call_kwargs[1].get("input") == "hello world"


class TestFormatResult:
    def test_success_with_output(self):
        result = _format_result(0, "data here\n", "")
        assert result == "data here\n"

    def test_success_no_output(self):
        result = _format_result(0, "", "")
        assert result == "(no output)"

    def test_success_whitespace_only(self):
        result = _format_result(0, "   \n", "")
        assert result == "(no output)"

    def test_failure_with_stderr(self):
        result = _format_result(1, "", "error msg\n")
        assert "[EXIT CODE 1]" in result
        assert "error msg" in result

    def test_failure_with_both(self):
        result = _format_result(2, "partial\n", "fatal\n")
        assert "[EXIT CODE 2]" in result
        assert "partial" in result
        assert "fatal" in result
