"""Invariants of this project, as executable checks.

Each rule here is one a plausible future change could break without any other
test noticing, and every list is derived from the code rather than written down
twice — a hardcoded list silently omits the next addition, which is the drift
these guards exist to stop.

Nothing runs these automatically; there is no CI. They run when someone runs
the suite, which is the gate this project actually has.
"""

import ast
import inspect
import pathlib
import re
import subprocess

import pytest

import ssh_hpc_server

ROOT = pathlib.Path(ssh_hpc_server.__file__).resolve().parent
SOURCE = pathlib.Path(ssh_hpc_server.__file__).resolve().read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _functions_containing(predicate) -> set[str]:
    """Names of module functions containing a node matching `predicate`."""
    found = set()
    for fn in ast.walk(TREE):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(predicate(node) for node in ast.walk(fn)):
                found.add(fn.name)
    return found


def _is_call_to(node, dotted: str) -> bool:
    parts = dotted.split(".")
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if len(parts) == 2:
        return (isinstance(f, ast.Attribute) and f.attr == parts[1]
                and isinstance(f.value, ast.Name) and f.value.id == parts[0])
    return isinstance(f, ast.Name) and f.id == parts[0]


def _tool_names() -> set[str]:
    """Tools as registered, taken from the decorators rather than a written list."""
    names = set()
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and any(
            _is_call_to(d, "mcp.tool") for d in node.decorator_list
        ):
            names.add(node.name)
    return names


# ---------------------------------------------------------------------------
# Trap: subprocess.run may appear in exactly two places
# ---------------------------------------------------------------------------
# Session reuse through the system ssh/scp binaries is the reason this project
# exists, and _run_raw is where every remote call is funnelled so that stdin
# isolation, UTF-8 replacement and the timeout note apply uniformly. A new tool
# calling subprocess.run directly would bypass all of it, silently.

class TestSubprocessBoundary:
    def test_only_two_functions_call_subprocess_run(self):
        callers = _functions_containing(lambda n: _is_call_to(n, "subprocess.run"))
        assert callers == {"_run_raw", "_local_openssh_version"}, callers

    def test_no_native_ssh_library_is_imported(self):
        imported = set()
        for node in ast.walk(TREE):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"paramiko", "fabric", "asyncssh", "globus_sdk", "pexpect"}
        assert not (imported & forbidden), imported & forbidden

    def test_the_globus_cli_is_not_a_dependency(self):
        """It is an optional external tool, invoked by name and checked for."""
        import tomllib
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        names = {re.split(r"[<>=!~\[ ]", d, maxsplit=1)[0].strip().lower()
                 for d in meta["project"]["dependencies"]}
        assert "globus-cli" not in names and "globus_sdk" not in names, names
        assert callable(ssh_hpc_server._globus_cli_available)


# ---------------------------------------------------------------------------
# Trap: the policy is registries keyed by string, which no type checker sees
# ---------------------------------------------------------------------------
# A misspelled tier is a KeyError in TIER_ORDER at call time, not at import,
# and only for the one command that trips that rule.



# ---------------------------------------------------------------------------
# Trap: the policy is registries keyed by string, which no type checker sees
# ---------------------------------------------------------------------------
# A misspelled tier is a KeyError in TIER_ORDER at call time, not at import,
# and only for the one command that trips that rule.

class TestPolicyRegistryIsWellFormed:
    def test_every_segment_rule_uses_a_known_tier(self):
        for pattern, tier, rule, roles in ssh_hpc_server._SEGMENT_RULES:
            assert tier in ssh_hpc_server.TIER_ORDER, (tier, rule)

    def test_every_segment_rule_uses_known_roles(self):
        valid = set(ssh_hpc_server.VALID_ROLES)
        for pattern, tier, rule, roles in ssh_hpc_server._SEGMENT_RULES:
            if roles is not None:
                assert set(roles) <= valid, (rule, roles)

    def test_every_callable_rule_uses_known_roles(self):
        valid = set(ssh_hpc_server.VALID_ROLES)
        for func, roles in ssh_hpc_server._CALLABLE_RULES:
            assert callable(func), func
            if roles is not None:
                assert set(roles) <= valid, (func.__name__, roles)

    def test_every_segment_rule_has_a_description(self):
        for pattern, tier, rule, roles in ssh_hpc_server._SEGMENT_RULES:
            assert rule and isinstance(rule, str), (tier, rule)

    def test_center_map_points_at_real_schedulers(self):
        assert set(ssh_hpc_server.CENTER_SCHEDULERS.values()) <= set(ssh_hpc_server.SCHEDULERS)

    def test_role_aliases_resolve_to_real_roles(self):
        assert set(ssh_hpc_server._ROLE_ALIASES.values()) <= set(ssh_hpc_server.VALID_ROLES)

    def test_every_callable_rule_returns_a_known_tier_or_none(self):
        """Exercised over the corpus of commands the rules are written for."""
        corpus = [
            "rm -rf /glade/work/me/run1", "find /glade -name x", "make -j",
            "python3 train.py", "gcc -o a a.c", "tar -czf a.tgz d", "ls -l",
        ]
        for func, _ in ssh_hpc_server._CALLABLE_RULES:
            for command in corpus:
                hit = func(command)
                if hit is not None:
                    tier, rule = hit
                    assert tier in ssh_hpc_server.TIER_ORDER, (func.__name__, tier)
                    assert isinstance(rule, str) and rule, (func.__name__, rule)


# ---------------------------------------------------------------------------
# Trap: the settings vocabulary is written in three places
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Trap: the settings vocabulary is written in three places
# ---------------------------------------------------------------------------

class TestSettingsVocabularyAgrees:
    def test_setting_keys_match_record_host_parameters(self):
        params = set(inspect.signature(ssh_hpc_server.record_host).parameters)
        params.discard("host")
        # `is_hpc` is the boolean form of the stored `hpc` key.
        expected = (params - {"is_hpc"}) | {"hpc"}
        assert set(ssh_hpc_server._SETTING_KEYS) == expected, (
            set(ssh_hpc_server._SETTING_KEYS) ^ expected
        )

    def test_policy_modes_are_all_handled(self):
        """Every mode must reach a distinct branch of _policy_refusal."""
        outcomes = {
            mode: ssh_hpc_server._policy_refusal("sudo ls", "login", False, False, mode=mode)
            for mode in ssh_hpc_server.POLICY_MODES
        }
        assert outcomes["off"] is None
        assert "Blocked by policy" in outcomes["strict"]
        assert "confirmation" in outcomes["permissive"]


# ---------------------------------------------------------------------------
# Trap: docs must list every tool, and drift the moment one is added
# ---------------------------------------------------------------------------

def _readme_tool_table() -> set[str]:
    """Names in the README's tool table, parsed from the table itself.

    Checking "appears somewhere in the README" is not a guard: a tool named in
    prose or in the changelog satisfies it even after its table row is gone.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^## Tools$(.*?)^## ", readme, re.M | re.S)
    assert m, "README.md has no '## Tools' section"
    return set(re.findall(r"^\| `(\w+)` \|", m.group(1), re.M))




class TestDocsListEveryTool:
    def test_the_readme_table_matches_the_registered_tools(self):
        documented, registered = _readme_tool_table(), _tool_names()
        assert documented == registered, {
            "undocumented": sorted(registered - documented),
            "stale row": sorted(documented - registered),
        }

    def test_every_tool_carries_annotations(self):
        for node in TREE.body:
            if isinstance(node, ast.FunctionDef):
                for d in node.decorator_list:
                    if _is_call_to(d, "mcp.tool"):
                        kwargs = {k.arg for k in d.keywords}
                        assert "annotations" in kwargs, node.name


# ---------------------------------------------------------------------------
# Trap: an environment variable added without a row in the docs
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Trap: .gitignore does not untrack
# ---------------------------------------------------------------------------
# A file added before its ignore rule existed stays tracked forever and git says
# nothing about it. Ask git, rather than restating the ignore list here.

class TestNothingTrackedIsIgnored:
    def test_no_tracked_file_matches_an_ignore_rule(self):
        result = subprocess.run(
            ["git", "ls-files", "-i", "-c", "--exclude-standard"],
            cwd=ROOT, capture_output=True, encoding="utf-8",
        )
        if result.returncode != 0:
            pytest.skip("not a git checkout")
        offenders = [line for line in result.stdout.splitlines() if line.strip()]
        assert not offenders, offenders

    def test_readme_is_the_only_documentation_in_the_repository(self):
        """Not just out of the release archive -- out of the repository, so it is
        absent from every remote and every clone."""
        result = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, encoding="utf-8",
        )
        if result.returncode != 0:
            pytest.skip("not a git checkout")
        tracked = {p for p in result.stdout.split() if p.endswith(".md")}
        assert tracked == {"README.md"}, tracked




class TestTheModuleHasNotOutgrownOneFile:
    """The single-file layout is deliberate, but it has a limit.

    The single-file layout is deliberate, but it has a limit: past ~3,000 lines
    the policy engine should move to its own module so the band boundary can be
    checked by an import scan instead of a grep for subprocess.run.
    """

    def test_under_the_three_thousand_line_threshold(self):
        lines = len(SOURCE.splitlines())
        assert lines < 3000, (
            f"{lines} lines: extract the policy engine (the '# Command policy' "
            "band) into its own module before this reaches 3,000."
        )




class TestPackageMetadataIsComplete:
    """What someone sees on a package index, not just in the repository.

    README.md is the only documentation that ships, so it has to actually reach
    the distribution metadata — otherwise the one public document is present as
    a file and absent from the page anyone browsing the package would read.
    """

    def test_the_readme_is_declared_as_the_long_description(self):
        import tomllib
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert meta["project"].get("readme") == "README.md", meta["project"].get("readme")

    def test_the_declared_readme_actually_ships(self):
        """Declaring a readme the distribution does not carry breaks the build."""
        import tomllib
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        readme = meta["project"]["readme"]
        assert (ROOT / readme).exists(), readme
        sdist = meta.get("tool", {}).get("hatch", {}).get(
            "build", {}).get("targets", {}).get("sdist", {})
        assert readme not in set(sdist.get("exclude", [])), readme

    def test_the_installed_metadata_carries_it(self):
        import importlib.metadata as md
        try:
            meta = md.metadata("hpc-ssh-mcp")
        except md.PackageNotFoundError:
            pytest.skip("package not installed")
        assert meta.get("Description-Content-Type") == "text/markdown"
        body = meta.get_payload() or meta.get("Description") or ""
        assert len(body) > 1000, len(body)


# ---------------------------------------------------------------------------
# Trap: the poll limiter must never wrap a mutating tool
# ---------------------------------------------------------------------------
# Returning a cached answer for a read is rate limiting; returning one for a
# write means the write silently did not happen.



# ---------------------------------------------------------------------------
# Trap: the poll limiter must never wrap a mutating tool
# ---------------------------------------------------------------------------
# Returning a cached answer for a read is rate limiting; returning one for a
# write means the write silently did not happen.

class TestCachedPollWrapsOnlyReads:
    def test_only_read_only_tools_call_cached_poll(self):
        callers = _functions_containing(lambda n: _is_call_to(n, "_cached_poll"))
        assert callers == {"check_job", "list_queue"}, callers

    def test_those_callers_are_annotated_read_only(self):
        for node in TREE.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"check_job", "list_queue"}:
                for d in node.decorator_list:
                    if _is_call_to(d, "mcp.tool"):
                        arg = next(k.value for k in d.keywords if k.arg == "annotations")
                        assert isinstance(arg, ast.Name) and arg.id == "_READ_ONLY", node.name


# ---------------------------------------------------------------------------
# Trap: the harness itself going stale
# ---------------------------------------------------------------------------

