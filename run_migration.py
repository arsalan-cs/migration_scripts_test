#!/usr/bin/env python3
"""
Run the Skyflow migration_scripts locally, without GitHub Actions.

Run it with:

    python run_migration.py          (Windows: py -3 run_migration.py)

It finds the migrate_*.py scripts in one of two places, in this order:

  1. its own directory -- which is how a customer uses it: drop this one file
     into the root of their migration_scripts copy, nothing else changes
  2. a sibling ../../migration_scripts clone -- which is how it runs from this
     lab folder during testing, with the repo left untouched

So during testing there is nothing to move or copy.

It does exactly what the GitHub workflows do -- nothing more. The migration
scripts themselves are unchanged and unmodified; this only sets the environment
variables the workflow YAML would have set, then runs them in order.

Why this exists: the workflows are the only GitHub-specific part of the repo.
The Python reads plain os.getenv, so it runs anywhere. Three things the YAML
does that the Python does not, and this handles:

  1. maps the "Source: X, Target: Y" choice to the two management URLs
  2. exports every input as an environment variable
  3. gives migrate_vault_schema.py a GITHUB_ENV file to write TARGET_VAULT_ID
     into, so the governance step can pick it up

Requires: Python 3.8+, and `pip install requests`.
"""

import getpass
import os
import subprocess
import sys
import tempfile

ENV_URLS = {
    "SANDBOX": "https://manage.skyflowapis-preview.com",
    "PRODUCTION": "https://manage.skyflowapis.com",
}

# Each entry: label -> (script filename, [required env var names])
MIGRATIONS = {
    "1": ("Vault schema only", "migrate_vault_schema.py",
          ["SOURCE_VAULT_ID", "WORKSPACE_ID"]),
    "2": ("Vault schema + roles and policies", None,
          ["SOURCE_VAULT_ID", "WORKSPACE_ID"]),      # two scripts, chained
    "3": ("Vault roles and policies only", "migrate_vault_roles_and_policies.py",
          ["SOURCE_VAULT_ID", "TARGET_VAULT_ID"]),
    "4": ("Connections", "migrate_connections.py",
          ["TARGET_VAULT_ID"]),
    "5": ("Roles", "migrate_roles.py", ["TARGET_VAULT_ID"]),
    "6": ("Policies", "migrate_policies.py", ["TARGET_VAULT_ID"]),
    "7": ("Service accounts", "migrate_service_accounts.py", ["TARGET_VAULT_ID"]),
}


def check_requests():
    """The scripts need `requests`. The workflows pip-install it inline, so the
    dependency is invisible until you run locally. There is no requirements.txt
    or pyproject.toml in this repo, so `pip install .` does not work."""
    import importlib.util
    if importlib.util.find_spec("requests") is not None:
        return
    print("The 'requests' package is not installed. Install it with:\n")
    print(f"    {os.path.basename(sys.executable)} -m pip install requests\n")
    print("If that fails behind a corporate proxy, ask your platform team for")
    print("an internal PyPI mirror or an offline wheel -- the migration scripts")
    print("cannot run without it.")
    sys.exit(1)


def scripts_dir():
    """Where the migrate_*.py files live. Defaults to this file's directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "migrate_vault_schema.py")):
        return here
    # Running from the lab folder during testing: fall back to a sibling clone.
    guess = os.path.abspath(os.path.join(here, "..", "..", "migration_scripts"))
    if os.path.exists(os.path.join(guess, "migrate_vault_schema.py")):
        return guess
    sys.exit("Cannot find migrate_vault_schema.py. Put this file in the root of "
             "your migration_scripts copy.")


def ask(prompt, default=None, secret=False, required=True):
    suffix = f" [{default}]" if default else ""
    while True:
        value = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        if not value and default:
            return default
        if value or not required:
            return value
        print("  required.")


def ask_id_list(prompt):
    """These scripts parse their ID lists with ast.literal_eval, so the value
    must be a Python literal: ['abc','def']. An empty list is rejected here --
    it would iterate nothing and still report success."""
    import ast
    while True:
        raw = ask(f"  {prompt}, e.g. ['abc','def']")
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            print("    not a valid Python list -- use ['abc','def'] with quotes.")
            continue
        if not isinstance(parsed, (list, tuple)):
            print("    must be a list.")
            continue
        if not parsed:
            print("    empty list would migrate nothing and still report success.")
            continue
        return raw


def choose_environments():
    print("\nWhich direction?")
    print("  1  Source: SANDBOX,    Target: PRODUCTION")
    print("  2  Source: SANDBOX,    Target: SANDBOX")
    print("  3  Source: PRODUCTION, Target: PRODUCTION")
    print("  4  Source: PRODUCTION, Target: SANDBOX")
    pairs = {"1": ("SANDBOX", "PRODUCTION"), "2": ("SANDBOX", "SANDBOX"),
             "3": ("PRODUCTION", "PRODUCTION"), "4": ("PRODUCTION", "SANDBOX")}
    while True:
        choice = input("choice: ").strip()
        if choice in pairs:
            return pairs[choice]
        print("  pick 1-4.")


def run(script, env, cwd):
    """Run one migration script as a subprocess, exactly as the workflow does."""
    print(f"\n{'=' * 70}\nRunning {script}\n{'=' * 70}")
    # sys.executable keeps us on the same interpreter the user invoked, which
    # matters on Windows where `python` may not be on PATH at all.
    result = subprocess.run([sys.executable, script], env=env, cwd=cwd)
    if result.returncode != 0:
        print(f"\n{script} exited {result.returncode}. Stopping.")
        print("NOTE: a failure here does not always mean nothing changed --")
        print("migrate_vault_schema.py creates the vault BEFORE the step that")
        print("can fail. Check the target account before re-running.")
        sys.exit(result.returncode)
    return result


def main():
    check_requests()
    root = scripts_dir()
    print(f"migration_scripts: {root}")

    print("\nWhat do you want to migrate?")
    for key, (label, _, _) in MIGRATIONS.items():
        print(f"  {key}  {label}")
    choice = input("choice: ").strip()
    if choice not in MIGRATIONS:
        sys.exit("unknown choice")
    label, script, required = MIGRATIONS[choice]

    source_env, target_env = choose_environments()
    print(f"\n{label}: {source_env} -> {target_env}")

    env = os.environ.copy()
    env["SOURCE_ENV_URL"] = ENV_URLS[source_env]
    env["TARGET_ENV_URL"] = ENV_URLS[target_env]

    print("\nAccount IDs (Studio -> account settings):")
    env["SOURCE_ACCOUNT_ID"] = ask("  source account ID")
    env["TARGET_ACCOUNT_ID"] = ask("  target account ID")

    print("\nResource IDs:")
    for var in required:
        env[var] = ask(f"  {var}")

    # Optional inputs the workflows expose. Empty string is fine -- the scripts
    # check truthiness, and "" is falsy. Do NOT type the word None here: the
    # workflow dropdown's literal "None" default is truthy in Python and is why
    # vaults come out named UntitledVault<random>.
    if choice in ("1", "2"):
        env["VAULT_NAME"] = ask("  vault name for the target (blank = copy source)",
                                required=False)
        env["VAULT_DESCRIPTION"] = ask("  vault description", required=False)

    if choice == "4":
        all_conns = ask("  migrate ALL connections in the source vault? (y/n)",
                        default="y").lower().startswith("y")
        if all_conns:
            env["MIGRATE_ALL_CONNECTIONS"] = "true"
            env["SOURCE_VAULT_ID"] = ask("  source vault ID")
        else:
            ids = ask("  connection IDs, e.g. ['abc','def']")
            env["CONNECTION_IDS"] = ids
            # The no-op trap: an empty list iterates nothing and still prints
            # "executed successfully" with a zero count.
            if ids.strip() in ("[]", ""):
                sys.exit("An empty connection list would report success having "
                         "migrated nothing. Supply IDs or choose 'all'.")

    if choice == "5":
        all_roles = ask("  migrate ALL custom roles of a source vault? (y/n)",
                        default="n").lower().startswith("y")
        if all_roles:
            env["MIGRATE_ALL_ROLES"] = "true"
            env["SOURCE_VAULT_ID"] = ask("  source vault ID")
        else:
            env["ROLE_IDS"] = ask_id_list("role IDs")
        if ask("  skip creating a role if one with that name already exists "
               "on the target vault? (y/n)", default="n").lower().startswith("y"):
            env["SKIP_ROLE_CREATION_IF_ROLE_EXISTS"] = "true"

    if choice == "6":
        env["POLICY_IDS"] = ask_id_list("policy IDs")

    if choice == "7":
        env["SERVICE_ACCOUNT_IDS"] = ask_id_list("service account IDs")

    print("\nBearer tokens (input hidden). Both are account-level Studio tokens.")
    print("The TARGET token must be workspace-scoped or admin -- a vault-scoped")
    print("service account cannot create a vault and fails with an opaque 403.")
    env["SOURCE_ACCOUNT_AUTH"] = ask("  source token", secret=True)
    env["TARGET_ACCOUNT_AUTH"] = ask("  target token", secret=True)

    # migrate_vault_schema.py writes TARGET_VAULT_ID into the file named by
    # GITHUB_ENV so the governance step can read it. GITHUB_ENV is just a
    # filename, so a temp file works identically off GitHub. If MIGRATE_GOVERNANCE
    # is set and this is missing, open(None) raises AFTER the vault is created.
    handoff = os.path.join(tempfile.gettempdir(), "skyflow_migration_env")
    open(handoff, "w").close()
    env["GITHUB_ENV"] = handoff

    if choice == "2":
        env["MIGRATE_GOVERNANCE"] = "true"
        run("migrate_vault_schema.py", env, root)

        # Pick up TARGET_VAULT_ID the first script just wrote.
        with open(handoff) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    env[k] = v
        if not env.get("TARGET_VAULT_ID"):
            sys.exit("Schema migration did not report a TARGET_VAULT_ID. "
                     "Check the output above before running governance.")
        print(f"\nnew target vault: {env['TARGET_VAULT_ID']}")
        run("migrate_vault_roles_and_policies.py", env, root)
    else:
        run(script, env, root)

    print("\nDone. Verify in the target account before treating this as complete "
          "-- some scripts report success having migrated nothing.")


if __name__ == "__main__":
    main()
