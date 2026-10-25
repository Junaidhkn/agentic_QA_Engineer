# coding-tools (as built)

The plan called for a separate coding-session image (repo checkout + allowlisted commands). As
built, the repair agent never gets a shell or a checkout:

- **Editing** happens in the control plane on an in-memory copy of the frozen source archive
  (`agents/repair/workspace.py`), through policy-checked tools (`read_file`, `search_code`,
  `write_file`, `apply_patch`). Edits become a patch.
- **Running** happens in `repair_check` runner jobs: the runner builds the app's own sandbox
  (same image, database, migrations and seed as a sandbox run) from base+patch and executes a
  fixed list of commands derived from the repository (`agents/repair/commands.py`: pytest, jest,
  vitest or `node --test`) inside the app container. The runner refuses any executable outside
  `ALLOWED_EXECUTABLES` (`runner_agent/sandbox_env/base.py`); there is no shell.
- The Python sandbox template installs `pytest` when the app does not, so regression tests run
  in Docker sandboxes too; Node apps use their own devDependencies.

Network, Git and API access are therefore never available to model-chosen actions.
