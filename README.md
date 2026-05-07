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
      "command": "uv",
      "args": ["run", "--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
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

0.3.0
