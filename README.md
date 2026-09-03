# hpc-ssh-mcp

[![GitHub](https://img.shields.io/github/v/tag/steathy/hpc-ssh-mcp)](https://github.com/steathy/hpc-ssh-mcp)

An MCP server that lets a coding agent drive SSH hosts and HPC schedulers: NSF NCAR Derecho and Casper (PBS Pro), CU Boulder Research Computing Alpine (Slurm), data-transfer nodes, Globus, or any host in your `~/.ssh/config`.

It shells out to the system `ssh` and `scp` binaries rather than using a Python SSH library, and that choice is the whole design. Every connection reuses your existing **SSH ControlMaster** socket, so multi-factor authentication happens once at your terminal instead of on every tool call.

> **Read [SSH multiplexing](#ssh-multiplexing-the-part-that-makes-this-work) before installing.** Without a working ControlMaster socket, every command would trigger a fresh Duo push, and the server has no terminal to answer it with. Multiplexing is not an optimization here; it is the mechanism.

## Contents

- [SSH multiplexing](#ssh-multiplexing-the-part-that-makes-this-work)
- [Platform support: Windows needs WSL](#platform-support-windows-needs-wsl)
- [Install](#install)
- [Connect your coding agent](#connect-your-coding-agent)
- [Tools](#tools)
- [Command policy](#command-policy)
- [Host profiles](#host-profiles)
- [Globus transfers](#globus-transfers)
- [Testing](#testing)
- [Changelog](#changelog)

## SSH multiplexing: the part that makes this work

HPC centers protect login nodes with two-factor authentication. NSF NCAR uses Duo; CU Boulder uses its own second factor. A second factor is designed to require a human, and an MCP server has no terminal to prompt at.

OpenSSH solves this with **connection multiplexing**. The first connection opens a long-lived *master* and leaves a control socket behind. Every later connection to the same host travels down that socket, skipping authentication entirely. You answer Duo once; the agent then runs hundreds of commands over the same authenticated channel.

### 1. Configure it

Add the multiplexing options to each HPC host in `~/.ssh/config`:

```sshconfig
Host derecho
    HostName derecho.hpc.ucar.edu
    User yourusername
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 48h

Host casper
    HostName casper.hpc.ucar.edu
    User yourusername
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 48h

Host ncar-data
    HostName data-access.ucar.edu
    User yourusername
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 48h

Host cu-alpine
    HostName login.rc.colorado.edu
    User yourusername
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 48h
```

What each option does:

| Option | Meaning |
|---|---|
| `ControlMaster auto` | Reuse a running master if there is one, otherwise become the master. |
| `ControlPath` | Where the control socket lives. `%r@%h-%p` expands to user@host-port, giving one socket per destination. |
| `ControlPersist 48h` | Keep the master alive for 48 hours after the first connection exits. Without it, the socket dies with your first shell. |

Create the socket directory once, and keep it private:

```bash
mkdir -p ~/.ssh/sockets && chmod 700 ~/.ssh/sockets
```

If `ssh` complains that the control path is too long, the socket path exceeded the operating system limit of about 104 characters. Use a shorter directory, or `ControlPath ~/.ssh/sockets/%C`, which is a fixed-length hash.

### 2. Open the master, once per session

From your own terminal, where you can answer the Duo prompt:

```bash
ssh -fN derecho
```

`-f` backgrounds the process after authenticating and `-N` runs no command, so this exists purely to hold the connection open. Answer Duo, and you are set for the next 48 hours.

### 3. Verify it

```bash
ssh -O check derecho     # -> Master running (pid=12345)
```

The agent can run this too, through the `check_ssh_connection` tool. If it reports a live master, every other tool will work without prompting.

### 4. When it breaks

The master dies if you reboot, change networks, or let `ControlPersist` expire. Every tool then fails within ten seconds and tells you what to do, rather than hanging:

```
[EXIT CODE 255]
stderr:
Permission denied (publickey,keyboard-interactive).

Hint: SSH auth failed for 'derecho'. The ControlMaster socket has likely expired...
From your terminal (where Duo/MFA prompts can be answered), run:
    ssh -fN derecho
Then retry.
```

To close a master deliberately:

```bash
ssh -O exit derecho
```

Two options are applied to every connection the server opens: `BatchMode=yes`, so SSH never tries to prompt, and `ConnectTimeout=10`, so an unreachable host fails in seconds rather than minutes.

## Platform support: Windows needs WSL

| Platform | Status |
|---|---|
| Linux | Fully supported. |
| macOS | Fully supported. |
| **Windows, native** | **Not supported.** |
| Windows via WSL | Fully supported. Use this. |

Microsoft's Win32 port of OpenSSH does not implement connection multiplexing. `ControlMaster` and `ControlPath` are accepted in the config file but no master is ever created, because Windows does not provide the Unix domain socket the control path needs.

The consequence is not slowness, it is that the server cannot work at all: every command would attempt a fresh authentication, Duo would prompt with nobody to answer, and the call would fail. Third-party Windows clients such as PuTTY have their own multiplexing schemes, but `ssh.exe` is what this server invokes.

**Windows users should run everything inside WSL.** Install WSL 2, install `uv` and your coding agent *inside* the WSL distribution, keep `~/.ssh/config` in WSL's home directory rather than on the Windows filesystem, and launch the agent from a WSL shell. Putting the SSH config or the control socket on a Windows drive under `/mnt/c` is known to hang the socket, so keep both on the Linux filesystem.

## Install

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and an OpenSSH client.

Most agents can run the server straight from GitHub with `uvx`, which downloads and caches it on first use. There is nothing to install by hand:

```bash
uvx --from git+https://github.com/steathy/hpc-ssh-mcp.git hpc-ssh-mcp
```

To develop against a local checkout instead:

```bash
git clone git@github.com:steathy/hpc-ssh-mcp.git
cd hpc-ssh-mcp
uv pip install -e ".[dev]"
uv run ssh_hpc_server.py
```

## Connect your coding agent

Every example below configures the same stdio server. Two things differ between products: which file, and whether the top-level key is `mcpServers`, `mcp_servers` or `servers`. After configuring, restart the agent and confirm the server connected before relying on it.

### Claude Code (CLI)

```bash
claude mcp add ssh-hpc -- uvx --from git+https://github.com/steathy/hpc-ssh-mcp.git hpc-ssh-mcp
```

Everything after `--` is the command to launch, and the separator is required. Add `--scope user` to make the server available in every project, or `--scope project` to write a `.mcp.json` you commit for your team; the default scope is this project only. Check it with `claude mcp list`, or `/mcp` inside a session.

The equivalent `.mcp.json`, if you would rather write it by hand:

```json
{
  "mcpServers": {
    "ssh-hpc": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
    }
  }
}
```

### Claude Code and Codex in VS Code

Neither extension has its own MCP configuration. Both read whatever their CLI is configured with, so run the `claude mcp add` or `codex mcp add` command in VS Code's integrated terminal and the extension picks it up. Manage it with `/mcp` in the chat panel.

VS Code's own MCP support, used by Copilot agent mode, is separate and uses `servers` rather than `mcpServers`. Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "ssh-hpc": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
    }
  }
}
```

Run **MCP: Add Server** from the command palette to write this for you, and **MCP: List Servers** to check it.

### Codex CLI

Codex uses TOML, and the key is `mcp_servers` with an underscore. Edit `~/.codex/config.toml`:

```toml
[mcp_servers.ssh-hpc]
command = "uvx"
args = ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
```

Or let the CLI write it, then check it:

```bash
codex mcp add ssh-hpc -- uvx --from git+https://github.com/steathy/hpc-ssh-mcp.git hpc-ssh-mcp
codex mcp list
```

### Antigravity: agy CLI and IDE

The Antigravity CLI, IDE and SDK share one configuration file, so this is a single setup for all three. Edit `~/.gemini/config/mcp_config.json` for every project, or `.agents/mcp_config.json` inside a workspace:

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

In the IDE the same file is reachable from the agent panel's MCP Servers view, under "View raw config". Type `/mcp` in the CLI prompt to open the manager and reload after editing.

### Gemini CLI

Edit `~/.gemini/settings.json` for every project, or `.gemini/settings.json` inside one:

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

Check it with `/mcp` inside a session.

### Claude Desktop

Edit the config file, creating it if it does not exist, then **quit and reopen the app completely**: it reads the file only at startup.

| Platform | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`, but see [Platform support](#platform-support-windows-needs-wsl) |

```json
{
  "mcpServers": {
    "ssh-hpc": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
    }
  }
}
```

Desktop applications do not inherit your shell's `PATH`, so if the app reports that `uvx` was not found, give the absolute path instead. Run `which uvx` to find it, typically `~/.local/bin/uvx`.

### Cursor

Edit `~/.cursor/mcp.json` for every project, or `.cursor/mcp.json` inside one:

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

### Passing settings to the server

Every product above accepts an `env` object beside `command` and `args`. In Codex's TOML it is `env = { KEY = "value" }`; Claude Code also takes `--env KEY=value` on the command line. Two variables are worth knowing:

```json
"env": {
  "HPC_SSH_MCP_CONFIG": "/home/you/.config/hpc-ssh-mcp/hosts.toml",
  "HPC_SSH_MCP_POLICY": "strict"
}
```

`HPC_SSH_MCP_CONFIG` points at your [host profiles](#host-profiles); `HPC_SSH_MCP_POLICY` sets the [command policy](#command-policy) for the session.

## Tools

| Tool | Description |
|---|---|
| `execute_remote_bash` | Run a bash command or multi-line script on a remote host (delivered on stdin to `bash -s`) |
| `run_on_compute` | Run one command on a compute node and wait: NCAR `qcmd` on PBS, `srun` on Slurm |
| `submit_job` | Write a batch script and submit it: `qsub` on PBS, `sbatch` on Slurm |
| `check_job` | Job status including finished jobs: `qstat -x` on PBS, `squeue` + `sacct` on Slurm |
| `list_queue` | Jobs in the queue for a user: `qstat -w -u` on PBS, `squeue -u` on Slurm |
| `cancel_job` | Cancel a job: `qdel` on PBS, `scancel` on Slurm |
| `read_remote_file` | Read a remote text file into context (capped at 200 KB, binary refused) |
| `tail_remote_file` | Read last N lines of a remote file |
| `scp_download_file` | Download a file via `scp` |
| `scp_upload_file` | Upload a file via `scp` |
| `check_ssh_connection` | Verify the ControlMaster socket is alive |
| `globus_status` | Which Globus identity this machine is logged in as |
| `globus_find_collection` | Search Globus for a collection UUID by name |
| `globus_ls` | List a directory on a Globus collection |
| `globus_transfer` | Submit a Globus transfer and return its task ID |
| `globus_task_status` | Status of a transfer task, with the last error if it failed |
| `globus_task_cancel` | Cancel a running transfer task |

Read-only tools carry the MCP `readOnlyHint` and `idempotentHint` annotations; the mutating ones carry `destructiveHint`, so a client can auto-approve reads and confirm the rest.

### Scheduler support

The job tools take `scheduler="auto"|"pbs"|"slurm"`. With `auto` the server probes the host once for `qsub` and `sbatch` and caches the answer, or skips the probe entirely when a [host profile](#host-profiles) names the center. Job IDs are validated per scheduler: `2426690.desched1` and `123[].desched1` on PBS, `12345`, `12345_0` and `12345.0` on Slurm.

`submit_job` accepts `remote_dir` so scripts are written to, and submitted from, a scratch or work directory rather than `$HOME`.

## Command policy

Every command sent through `execute_remote_bash` or `run_on_compute` is sorted into a tier first. This is not caution for its own sake. NSF NCAR publishes [guidance for agentic AI coding assistants](https://ncar-hpc-docs.readthedocs.io/en/latest/best-practices-for-supercomputer-users/agentic-ai/) covering Derecho and Casper, CU Boulder states that login nodes are "not intended for computational tasks of any kind", and both centers terminate offending processes without notice. You remain fully responsible for what an agent does under your account.

| Tier | Examples | What happens |
|---|---|---|
| **block** | `sudo`, `su`, `apt`/`yum`/`dnf`; `rm -rf` on `/`, `~`, `$HOME`, `/glade`, `/scratch`, `/pl`, `/projects`; recursive traversal (`find`, `du`, `ls -R`, `grep -r`, `rg`, `tree`) at or above a shared root; fork bombs; `mkfs`; `dd of=/dev/...`; writes to `authorized_keys` | Refused. |
| **confirm** | `rm -r` elsewhere, `find -delete`, recursive `chmod`/`chown`, `chmod 777`, unbounded `make -j`, `git push --force`, `git reset --hard`, `scancel -u`, `qdel $(qselect ...)`, `truncate`, `shred`, `crontab`, `ssh-keygen`, redirects over `.nc`/`.h5` files | Refused unless called with `confirm_destructive=true`. The agent has to ask you first. |
| **route** | interpreters running a script, compilers and `make`, MPI launchers, NCO/CDO tools, `conda`/`pip install`, `tar`/`zip`, `rsync`, `jupyter`, `nohup`, `tail -f`, `watch` | On a login or data-access node, refused with a pointer to `run_on_compute`; `allow_on_login_node=true` overrides for genuinely small cases. |
| **free** | `ls`, `cat`, `grep`, `qstat`, `squeue`, `module avail`, `python --version`, `git status` | Runs. |

The traversal rule is the one worth understanding, because it is easy to trip. NCAR asks agents to *never* run `find`, `lfs find`, `du`, `ls -R`, `tree`, `grep -r` or `rg` at or above `/glade`, `/glade/work`, `/glade/campaign`, `/glade/derecho/scratch` or any other shared root: those commands cause a metadata storm that slows the filesystem for everyone on the machine. Traversal inside your own directory is untouched, so `find /glade/work/$USER/run1 -name '*.nc'` is fine while `find /glade/work -name '*.nc'` is not.

Scheduler queries are rate-limited to one identical query per host every 30 seconds. Transfers over 2 GB get a note pointing at Globus or a data-transfer node.

### When you really do want to run a blocked command

Sometimes the guard is wrong, or you know exactly what you are doing and accept the risk. The override is deliberately **not a tool parameter**: a rail the agent can disable on its own is not a rail. Only you can relax it, either in `hosts.toml`:

```toml
[policy]
mode = "strict"        # default, applies to every host

[my-workstation]
policy = "off"         # a machine you own and administer yourself
```

or for a single session, by launching the server with an environment variable:

```json
"env": { "HPC_SSH_MCP_POLICY": "permissive" }
```

| Mode | Effect |
|---|---|
| `strict` | Default. The block tier is refused outright. |
| `permissive` | The block tier becomes a confirmation: the agent must ask you, then pass `confirm_destructive=true`. |
| `off` | No policy checks at all. Reasonable on your own workstation, not on a shared cluster. |

A refusal in strict mode tells you these options, so you never have to come back here to find them. `permissive` is the useful middle ground on a cluster: nothing is silently prevented, and nothing dangerous happens without you saying yes.

## Host profiles

An optional TOML file tells the server what each host is, so it skips the scheduler probe, defaults your account, and applies the right policy role. Copy [`hosts.example.toml`](hosts.example.toml) to `~/.config/hpc-ssh-mcp/hosts.toml`, or point `$HPC_SSH_MCP_CONFIG` at your own path.

```toml
[derecho]
center  = "ncar"                          # ncar -> PBS, curc -> Slurm
role    = "login"                         # login | data-access | compute | workstation
account = "UABC0001"                      # default -A for run_on_compute / submit_job
scratch = "/glade/derecho/scratch/$USER"  # suggested remote_dir for job output
```

`role = "data-access"` allows transfers while still routing compute away; `role = "workstation"` lifts login-node routing entirely for a machine you own. A missing or malformed file simply means no profiles.

## Globus transfers

Both centers ask for bulk data to move through Globus rather than `scp` over a login node. The Globus CLI talks to the Globus API, not to a cluster, so these tools run it **locally**: no login node is touched and no SSH session is needed. It is an optional external tool, never a dependency of this package.

Set it up once, in your own terminal. The server never handles your credentials:

```bash
uv tool install globus-cli
globus login
```

Mapped collections such as NCAR GLADE, NCAR Campaign Storage and CU Boulder Research Computing also need a one-time `data_access` consent each. You meet this the first time you list or transfer, and the tools print the exact command, taken verbatim from what Globus returns:

```
ConsentRequired: Missing required data_access consent.

Hint: this collection needs a one-time data_access consent, granted from your own terminal:
    globus session consent 'urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/scopes/d33b3614-.../data_access]'
Then retry.
```

Put the UUIDs you use often in `hosts.toml` so calls can name them:

```toml
[globus.collections]
glade  = "d33b3614-6d04-11e5-ba46-22000b92c6ec"   # NCAR GLADE
alpine = "..."                                     # CU Boulder Research Computing
```

A transfer is then `globus_transfer(source="glade", source_path="/glade/work/me/run1", dest="alpine", dest_path="/scratch/alpine/me/run1", recursive=True)`. Transfers default to `--sync-level mtime`, so re-running one is idempotent. Mirroring with `--delete-destination-extra` requires `confirm_destructive=true`.

## Testing

```bash
uv pip install -e ".[dev]"
uv run pytest tests/ -v

# Live tests against a real host (needs key or ControlMaster auth; leaves nothing behind)
HPC_SSH_MCP_TEST_HOST=<ssh alias> uv run pytest tests/test_integration.py -v
```

The unit suite mocks `subprocess.run`. The live suite exists because several real bugs were invisible to mocks: scp path quoting on modern OpenSSH, `ssh -O check` writing its verdict to stderr, and Globus reporting a missing consent with an exit code the code was not checking.

## Version

1.4.0

## Changelog

### 1.4.0

- **NSF NCAR agentic-AI rules.** Recursive traversal at or above a shared filesystem root is blocked; `chmod 777` and unbounded `make -j` need confirmation; `tail -f` and `watch` are routed off login nodes. Source: [NCAR's agentic AI guidance](https://ncar-hpc-docs.readthedocs.io/en/latest/best-practices-for-supercomputer-users/agentic-ai/).
- **A policy escape hatch you control.** `policy = "strict" | "permissive" | "off"` per host or globally in `hosts.toml`, or `HPC_SSH_MCP_POLICY` for one session. No tool takes a policy argument, so an agent can never relax its own guard rails.
- **Globus consent errors are handled.** Globus reports `ConsentRequired` with exit code 1 and a JSON body, not the auth exit code, so the consent hint never fired. Scopes are now taken verbatim from the error.
- **Documentation.** A full section on SSH multiplexing, an explicit statement that native Windows cannot work and WSL is required, and setup instructions for Claude Code, Codex CLI, Antigravity, Gemini CLI, Claude Desktop, VS Code and Cursor.

### 1.3.0

- **Globus transfer tools**: `globus_status`, `globus_find_collection`, `globus_ls`, `globus_transfer`, `globus_task_status`, `globus_task_cancel`.
- The CLI runs locally with `GLOBUS_CLI_INTERACTIVE=0` and JSON output; exit code 4 is translated into the `globus login` steps.
- Collection aliases come from `[globus.collections]` in `hosts.toml`; a bare UUID always works.
- If the Globus CLI is not installed, every Globus tool says so and explains how to install and log in.

### 1.2.0

- **Command policy tiers** on `execute_remote_bash` and `run_on_compute`, with `confirm_destructive` and `allow_on_login_node` flags.
- **Host profiles** (`~/.config/hpc-ssh-mcp/hosts.toml`): per-alias `center`, `role`, `account` and `scratch`.
- **Scheduler polling is rate-limited** to one identical query per host per 30 s.
- **Bulk-transfer notice** over 2 GB; a missing local file is reported before `scp` runs.
- **`submit_job` suggests the profile's scratch directory** when no `remote_dir` was given.
- Requires Python 3.11+ (for `tomllib`).

### 1.1.0

**Breaking:** `submit_slurm_job`, `check_slurm_job`, `list_slurm_queue` and `cancel_slurm_job` are replaced by the scheduler-aware `submit_job`, `check_job`, `list_queue` and `cancel_job`. The old tools only ever worked on Slurm; NCAR Derecho and Casper run PBS Pro.

- **PBS Pro support** with automatic scheduler detection, PBS job-ID validation, `remote_dir`, and one-round-trip `check_job`. New `run_on_compute` wraps NCAR's `qcmd` (or `srun`).
- **scp works on OpenSSH 9.0+.** Modern scp speaks SFTP and passes the remote path literally, so the old shell quoting broke every path with a space or `~`.
- **Child processes no longer inherit the MCP server's stdin**, which `ssh` was forwarding to the remote command.
- **Commands travel on stdin to `bash -s`** instead of through the remote login shell, so multi-line scripts and `!` work under tcsh too.
- **Non-UTF-8 output no longer crashes a tool.**
- **Diagnostic `ssh -fN` hints fire only on ssh's own exit 255.**
- **`check_ssh_connection` reports the real verdict**, which OpenSSH prints on stderr.
- **Local scp paths are made absolute**, so `:` is not parsed as a host and a leading `-` is not an option.
- **`--` before every path**, and `~/x` expanded to the remote home.
- **Output caps**: `read_remote_file` fetches at most `max_bytes` (default 200 KB) and refuses binary files.
- **scp tools take a `timeout`** (default 3600 s); a timed-out download removes the truncated file.
- **MCP tool annotations**; `fastmcp` pinned to `>=3,<4`.

### 1.0.0

- **Batch-safe SSH defaults.** Every `ssh` and `scp` invocation carries `-o BatchMode=yes -o ConnectTimeout=10`, so a dead ControlMaster socket fails in ten seconds with a parseable error instead of hanging.
- **Actionable failure diagnostics.** SSH failures are fingerprinted and rewritten with a hint telling you to run `ssh -fN <host>` to re-establish the multiplex socket out of band.

### 0.3.0

- Initial public baseline: 10 MCP tools wrapping native `ssh` / `scp` / `sbatch` / `squeue` / `sacct` / `scancel`.
