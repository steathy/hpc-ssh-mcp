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
- [Host settings](#host-settings)
- [Globus transfers](#globus-transfers)
- [Testing](#testing)
- [Changelog](#changelog)

## SSH multiplexing: the part that makes this work

HPC centers protect login nodes with two-factor authentication. A second factor is designed to require a human, and an MCP server has no terminal to prompt at.

OpenSSH solves this with **connection multiplexing**. The first connection opens a long-lived *master* and leaves a control socket behind. Every later connection to the same host travels down that socket, skipping authentication entirely. You answer Duo once; the agent then runs hundreds of commands over the same authenticated channel.

Add these three options to each HPC host in `~/.ssh/config`, and create the socket directory once with `mkdir -p ~/.ssh/sockets && chmod 700 ~/.ssh/sockets`:

```sshconfig
Host derecho
    HostName derecho.hpc.ucar.edu
    User yourusername
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 48h
```

| Option | Meaning |
|---|---|
| `ControlMaster auto` | Reuse a running master if there is one, otherwise become the master. |
| `ControlPath` | Where the control socket lives. `%r@%h-%p` gives one socket per destination. If SSH says the path is too long, use `%C`, a fixed-length hash. |
| `ControlPersist 48h` | Keep the master alive for 48 hours after the connection that created it exits. Without this the socket dies with your first shell. |

Then just connect normally: `ssh derecho`. Because of `ControlMaster auto`, that ordinary login *becomes* the master, and `ControlPersist` keeps it alive in the background after you log out. You do not need a special command. `ssh -fN derecho` does the same thing without giving you a shell, which is handy when you only want to re-arm the socket.

When the master expires or the machine sleeps, tools fail within ten seconds and say so, rather than hanging. Connect once more and carry on. `ssh -O check derecho` reports whether a master is running, and the agent can ask the same question through `check_ssh_connection`.

## Platform support: Windows needs WSL

| Platform | Status |
|---|---|
| Linux | Supported. |
| macOS | Supported. |
| **Windows, native** | **Not supported.** Win32 OpenSSH does not implement connection multiplexing, so every command would re-authenticate and Duo would prompt with nobody to answer. |
| Windows via WSL | Supported. Install `uv` and your agent inside the WSL distribution and keep `~/.ssh/config` on the Linux filesystem, not under `/mnt/c`. |

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
  "HPC_SSH_MCP_POLICY": "strict",
  "HPC_SSH_MCP_STORE": "/home/you/.config/hpc-ssh-mcp/hosts.json"
}
```

`HPC_SSH_MCP_POLICY` sets the [command policy](#command-policy) for the session. `HPC_SSH_MCP_STORE` moves the [settings file](#host-settings) `record_host` writes to.

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
| `probe_host` | Detect what a host is: scheduler, filesystems, account, Globus |
| `record_host` | Record what a host is, in the server's own settings file |
| `globus_status` | Which Globus identity this machine is logged in as |
| `globus_find_collection` | Search Globus for a collection UUID by name |
| `globus_ls` | List a directory on a Globus collection |
| `globus_transfer` | Submit a Globus transfer and return its task ID |
| `globus_task_status` | Status of a transfer task, with the last error if it failed |
| `globus_task_cancel` | Cancel a running transfer task |

Read-only tools carry the MCP `readOnlyHint` and `idempotentHint` annotations; the mutating ones carry `destructiveHint`, so a client can auto-approve reads and confirm the rest.

### Scheduler support

**You do not need to know which scheduler your cluster runs.** The job tools default to `scheduler="auto"`: the server asks the host once whether it has `qsub` or `sbatch`, caches the answer, and uses the matching commands. If neither exists it says so plainly.

To check for yourself, ask the host the same question:

```bash
ssh derecho 'command -v qsub sbatch'
```

Whichever path comes back is the scheduler. As a shortcut for the systems in this README:

| System | Scheduler | Submit | Queue | Cancel |
|---|---|---|---|---|
| NSF NCAR Derecho, Casper | PBS Pro | `qsub` | `qstat` | `qdel` |
| CU Boulder Alpine, Blanca | Slurm | `sbatch` | `squeue` | `scancel` |

Recording `center=ncar` or `center=curc` for the host skips the probe entirely. You can also pass `scheduler="pbs"` or `scheduler="slurm"` to any job tool to override both. Job IDs are validated per scheduler: `2426690.desched1` and `123[].desched1` on PBS, `12345`, `12345_0` and `12345.0` on Slurm.

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

Sometimes the guard is wrong, or you know exactly what you are doing and accept the risk. The refusal never points the agent at a way to lift it. Relax it yourself, either by giving the host a `policy` in the [settings file](#host-settings):

```json
{
  "hosts": {
    "shared-cluster": { "policy": "permissive" }
  }
}
```

For a machine that is not an HPC system at all, say so instead of loosening the policy: `"hpc": false` turns the whole thing off for that host, which is what it is for.

or for a single session, by launching the server with an environment variable:

```json
"env": { "HPC_SSH_MCP_POLICY": "permissive" }
```

| Mode | Effect |
|---|---|
| `strict` | Default. The block tier is refused outright. |
| `permissive` | The block tier becomes a confirmation: the agent must ask you, then pass `confirm_destructive=true`. |
| `off` | No policy checks at all. This is what `hpc=false` selects. |

A refusal in strict mode tells you these options, so you never have to come back here to find them. `permissive` is the useful middle ground on a cluster: nothing is silently prevented, and nothing dangerous happens without you saying yes.

## Host settings

A few things about a host cannot be discovered from the host itself, or are too slow to rediscover every session. They live in one small JSON file this server owns, keyed by the SSH alias you connect with:

```json
{
  "hosts": {
    "derecho": {
      "center": "ncar",
      "role": "login",
      "account": "UABC0001",
      "scratch": "/glade/derecho/scratch/$USER"
    },
    "laptop": { "hpc": false }
  }
}
```

It lives at `~/.config/hpc-ssh-mcp/hosts.json` (`HPC_SSH_MCP_STORE` moves it), and you never have to write it by hand — see [letting the agent fill this in](#letting-the-agent-fill-this-in). Every key is optional, and with nothing recorded the server probes for the scheduler and treats the host as an HPC login node, which is the cautious reading.

**`~/.ssh/config` is neither written nor read.** It is still what `ssh` itself uses to make the connection — aliases, `ControlMaster`, keys, all of it — but this server does not look inside it. Earlier versions kept these keys in a `# hpc-mcp:` comment in the matching `Host` block; that is gone, and a leftover comment is inert. See [1.9.0](#190) for how to move.

| Key | Values | Effect |
|---|---|---|
| `hpc` | `false` | This host is not a shared HPC system. Login-node etiquette and the whole [command policy](#command-policy) stop applying. |
| `center` | `ncar`, `curc` | Selects PBS or Slurm without probing the host. |
| `role` | `login`, `data-access`, `compute` | Sets the command policy tier. `data-access` allows transfers while still routing compute away. |
| `account` | project code | Default `-A` / `--account` for `run_on_compute`. |
| `scratch` | path | Suggested as `remote_dir` when `submit_job` is called without one. |
| `globus` | collection UUID | Lets Globus tools name this SSH alias instead of a UUID. |
| `policy` | `strict`, `permissive`, `off` | See [when you really do want to run a blocked command](#when-you-really-do-want-to-run-a-blocked-command). |

Aliases are matched exactly: a host is described because you described it, never because a wildcard happened to cover it. There is no defaults-for-everything entry — set `HPC_SSH_MCP_POLICY` if you want one policy for a whole session.

### Letting the agent fill this in

You do not have to write the file by hand. The first time a tool touches a host with nothing recorded, the result carries a one-line notice, and the agent can offer to sort it out:

1. `probe_host` connects and reports what it finds: the scheduler, which of the known HPC filesystems are mounted, any project code in the environment, and whether the Globus CLI is installed. It proposes settings and lists what it cannot detect.
2. The agent asks you the rest. Whether this really is a shared HPC system, which project code jobs should charge, and which policy level you want. It is told not to choose the policy for you.
3. `record_host` records your answers. Calling it again updates the keys you pass and leaves the rest alone, so relaxing one host's policy later does not erase its project code. To remove a setting, edit the file.

**It does not touch `~/.ssh/config`, and nothing here ever will.** That file controls access to every host you have. A mangled line, a replaced symlink from a dotfile manager, or a downgraded file mode would cost you far more than the convenience is worth, so the server leaves it entirely alone — it does not even read it.

Delete the settings file, edit it, or check it into nothing at all: the worst case is that hosts fall back to safe defaults. If it ever becomes unreadable, the server says so and refuses to rewrite it rather than discarding what it held.

Nothing is written until you have answered, and the notice is a nudge rather than a gate: an unrecorded host keeps working.

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

Record a collection UUID against the SSH alias for that system, and the tools accept the alias you already use:

```
record_host(host="derecho", globus="d33b3614-6d04-11e5-ba46-22000b92c6ec")
```

A transfer is then `globus_transfer(source="derecho", source_path="/glade/work/me/run1", dest="cu-alpine", dest_path="/scratch/alpine/me/run1", recursive=True)`. A bare UUID always works too, and `globus_find_collection` looks one up by name. Transfers default to `--sync-level mtime`, so re-running one is idempotent. Mirroring with `--delete-destination-extra` requires `confirm_destructive=true`.

## Testing

```bash
uv pip install -e ".[dev]"
uv run pytest tests/ -v

# Live tests against a real host (needs key or ControlMaster auth; leaves nothing behind)
HPC_SSH_MCP_TEST_HOST=<ssh alias> uv run pytest tests/test_integration.py -v
```

The unit suite mocks `subprocess.run`. The live suite exists because several real bugs were invisible to mocks: scp path quoting on modern OpenSSH, `ssh -O check` writing its verdict to stderr, and Globus reporting a missing consent with an exit code the code was not checking.

## Version

1.10.0

## Changelog

### 1.10.0

**No functional change to the server.** Packaging and tests only.

- **`README.md` is declared as the long description.** `pyproject.toml` had no `readme` key, so every built distribution carried the one-line summary and a blank body: on any index that renders a package page, this project's only document was present as a file and absent from the page.
- **`tests/test_harness_guards.py` turns the project's own invariants into checks** — that `subprocess.run` appears in exactly two functions, that no native SSH library is imported, that the policy tier and role tables only use values that exist, that `record_host`'s parameters and the stored setting keys agree, that the tool table in this README matches the registered tools, that every tool carries MCP annotations, that the poll cache wraps only read-only tools, and that the module has not outgrown its single file. Every list is derived from the code rather than restated, so a new addition cannot be silently omitted.

### 1.9.0

**Breaking: `~/.ssh/config` is no longer read, and `annotate_host` is now `record_host`.**

Host settings come from one place: `~/.config/hpc-ssh-mcp/hosts.json`. 1.7.0 stopped *writing* to `~/.ssh/config` but went on reading a `# hpc-mcp:` comment out of it, which was still the wrong call. It kept a parser for someone else's file format in this tree; it gave one word ("annotation") two meanings, one of them the dangerous behaviour that had already been removed; and because ssh patterns match by wildcard, a `Host *` block answered "yes, this host is described" for every alias you had never mentioned — which silenced the first-use notice for every host and made `probe_host` claim a brand-new host was already set up.

- **`~/.ssh/config` is neither written nor read.** It is still what `ssh` uses to connect; this server does not open it. `HPC_SSH_MCP_SSH_CONFIG` is gone.
- **To move:** for each host with a `# hpc-mcp:` comment, run `record_host(host="<alias>", ...)` with the same keys, or write the JSON yourself. The leftover comment is inert and `ssh` ignores it, so you can delete it whenever. `probe_host` will offer to do this for you on first contact.
- **A `Host *` default has no equivalent**, deliberately: aliases are matched exactly. Use `HPC_SSH_MCP_POLICY` for a session-wide policy.
- **`annotate_host` is renamed `record_host`**, and "annotation" is gone from the vocabulary — the file holds *settings*. MCP's own `ToolAnnotations` is a different thing and keeps its name.
- **The policy refusal no longer names a tool.** It points at the settings file and `HPC_SSH_MCP_POLICY`, so the escape stays the human's.
- **`account` is documented as `run_on_compute` only**, which is all it ever was; `submit_job` takes the account from the `#PBS -A` / `#SBATCH --account` line in your script.

Also in 1.9.0, from the same review:

- **A quoted argument is one token.** `grep -n 'rm -rf /' notes.md` and `grep -r "temperature in /glade" mydir/` were *blocked*: the policy split on whitespace, so a quoted search pattern supplied its own `-r` and its own path. That is a false positive in the one tier with no override.
- **A timed-out command says the remote command is still running.** The timeout kills the local `ssh`; with no TTY the remote side keeps going. Saying only "Timed out" invited a retry that stacked orphans on the shared node. The live suite proves the warning is true by watching the command finish after the client gave up.
- **`check_ssh_connection` explains a host with no `ControlPath`** instead of returning a bare exit 255. Not multiplexing is the normal state of a host that does not need MFA.
- **The output cap is a cap.** `stdout` and `stderr` were each truncated to 200 KB and then joined, so a failing command could return 400 KB.
- **The `authorized_keys` rule is anchored to a path component.** `echo hi > my_authorized_keys_notes.txt` was blocked.
- **Walltimes accept Slurm's `D-HH:MM:SS`**, so a job longer than a day can be asked for.
- **Housekeeping:** expired scheduler-poll entries are dropped rather than kept for the session; the settings file is written through a unique temporary name, so two concurrent writers cannot collide; `mcp` is declared as the direct dependency it is.

### 1.8.0

- **The settings file is JSON**, at `~/.config/hpc-ssh-mcp/hosts.json`. It is machine-written and machine-read, so it uses a format with a parser that already exists instead of a line syntax invented here. `hpc` is a real boolean. Hand-written annotations stay in `~/.ssh/config`, and still win.
- **A store that cannot be parsed is no longer silently overwritten.** `annotate_host` reports the problem and refuses, rather than discarding whatever the file held.
- **Fixed: a Globus UUID recorded by `annotate_host` was write-only.** Collection aliases only looked at `~/.ssh/config`, so a UUID in the store could never be resolved.

### 1.7.0

**`annotate_host` no longer writes to `~/.ssh/config`.** Editing the file that controls access to every one of your hosts was the wrong place to put a convenience, and it had real bugs: writing through a symlinked config replaced the symlink and orphaned the dotfile behind it, the new file landed with the umask default rather than the original mode, and the single backup slot was overwritten by the next write.

- Annotations now go to `~/.config/hpc-ssh-mcp/hosts.conf`, created private, atomically replaced, and disposable: deleting it only loses defaults. `HPC_SSH_MCP_STORE` moves it.
- `~/.ssh/config` is still read, and a `# hpc-mcp:` comment you write there by hand takes precedence over anything the server wrote, key by key.
- A `Host *` block no longer counts as ~/.ssh/config knowing an alias, so a typo'd host is flagged rather than silently accepted.

### 1.6.0

- **The agent can set a host up for you.** `probe_host` detects the scheduler, HPC filesystems, default account and Globus CLI, proposes an annotation, and lists what only you can answer. `annotate_host` writes the answers into `~/.ssh/config` after you confirm, keeping a backup and touching only that Host block. The first tool call against an unannotated host says so once.
- **`hpc=false` replaces the `workstation` role.** A machine that is not a shared HPC system is not a kind of HPC node, so it is now stated directly, and it lifts login-node etiquette and the command policy together. Roles are `login`, `data-access` and `compute`.

### 1.5.0

**Breaking:** the `hosts.toml` file is gone. Host metadata now lives in a `# hpc-mcp:` comment inside the matching `Host` block of `~/.ssh/config`, so there is one file instead of two and no alias is listed twice. `HPC_SSH_MCP_CONFIG` is replaced by `HPC_SSH_MCP_SSH_CONFIG`, which points at a non-standard SSH config. Globus collections are named by an annotated SSH alias rather than a separate alias table.

- `Host *` blocks and glob patterns work, resolved first-match-wins as SSH does.
- `Include` directives are followed.
- A missing, unreadable or malformed config is never an error; it just means no annotations.

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
