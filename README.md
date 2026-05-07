# hpc-ssh-mcp

MCP server for remote execution on SSH-enabled servers and supercomputers (e.g. NCAR Derecho).

Uses native `ssh`/`scp` binaries via `subprocess` to respect `~/.ssh/config` and `ControlMaster` multiplex sockets — no Duo MFA re-prompts.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An active SSH `ControlMaster` socket for your target host (configured in `~/.ssh/config`)

## Install

```bash
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
      "args": ["run", "--directory", "/path/to/hpc-ssh-mcp", "ssh_hpc_server.py"]
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
| `read_remote_file` | Read a remote text file into context |
| `scp_download_file` | Download a file via `scp` |

## Version

0.1.0
