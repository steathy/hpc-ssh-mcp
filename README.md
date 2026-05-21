# hpc-ssh-mcp

[![GitHub](https://img.shields.io/github/v/tag/steathy/hpc-ssh-mcp)](https://github.com/steathy/hpc-ssh-mcp)

MCP server for remote execution on SSH-enabled servers and supercomputers (e.g. NCAR Derecho).

Uses native `ssh`/`scp` binaries via `subprocess` to respect `~/.ssh/config` and `ControlMaster` multiplex sockets — no Duo MFA re-prompts.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An active SSH `ControlMaster` socket for your target host (configured in `~/.ssh/config`)

## Install

```bash
# Via uv (recommended)
uv pip install git+https://github.com/steathy/hpc-ssh-mcp.git

# Or clone and install locally
git clone git@github.com:steathy/hpc-ssh-mcp.git
cd hpc-ssh-mcp
uv pip install -e .
```

## Run

```bash
uv run ssh_hpc_server.py
```

## Claude Code integration

Add to your MCP config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ssh-hpc": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `execute_remote_bash` | Run any bash command on a remote host |
| `submit_slurm_job` | Write a Slurm batch script and submit via `sbatch` |
| `check_slurm_job` | Query job status via `squeue` + `sacct` |
| `cancel_slurm_job` | Cancel a Slurm job via `scancel` |
| `list_slurm_queue` | List jobs in the Slurm queue (`squeue -u $USER`) |
| `read_remote_file` | Read a remote text file into context |
| `tail_remote_file` | Read last N lines of a remote file |
| `scp_download_file` | Download a file via `scp` |
| `scp_upload_file` | Upload a file via `scp` |
| `check_ssh_connection` | Verify ControlMaster socket is alive |

## Testing

```bash
uv pip install -e ".[dev]"
uv run pytest tests/ -v
```

## Version

1.0.0

## Changelog

### 1.0.0

- **Batch-safe SSH defaults.** Every `ssh` and `scp` invocation now carries `-o BatchMode=yes -o ConnectTimeout=10`. Without these, a dead `ControlMaster` socket caused SSH to fall back to interactive auth — and because MCP servers have no controlling terminal, the process would either hang on the JSON-RPC stream until the 120 s timeout or fail with a cryptic message. With them, the failure is bounded to ≤ 10 s and surfaces a parseable error.
- **Actionable failure diagnostics.** SSH/SCP failures are now fingerprinted (`Permission denied (...keyboard-interactive...)`, `Control socket connect: No such file`, `Connection timed out`, `No route to host`) and rewritten with a hint telling the user exactly what to do — typically `ssh -fN <host>` from their terminal to re-establish the multiplex socket and complete Duo/MFA out-of-band.
- **`check_ssh_connection` is unchanged.** `ssh -O check` is a local socket query, not a connection, so it intentionally does not carry the new options.

### 0.3.0

- Initial public baseline: 10 MCP tools wrapping native `ssh` / `scp` / `sbatch` / `squeue` / `sacct` / `scancel`, leaning on `~/.ssh/config` + `ControlMaster` for credential reuse.
