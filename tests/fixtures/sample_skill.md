# Backport Skill File: sample-repo

## Branch Conventions
- `main` / `edge`: active development.
- `8.4/edge`: release branch for 8.4.
- `8.0/edge`: stable release branch for 8.0.
- Backport branches: `backport/<feature>-to-<target>`

## Lifecycle Hooks
- **Machine charm:** `install` -> `start` -> `config-changed`. The `start`
  hook calls `workload_initialise`. During charm upgrade (`juju refresh`),
  `start` fires but is deferred by `_can_start` if `upgrade.idle` is False.
  The upgrade framework handles workload lifecycle via `_on_upgrade_granted`
  in `upgrade.py`.

## Upgrade Path
- Machine charm upgrades: snap is refreshed in-place. The `start` hook is
  deferred during upgrade. Any new "enable X by default" change must also
  be added to `_on_upgrade_granted` in `upgrade.py` to take effect on
  existing deployments.

## Things to Check When Backporting
1. **Hook coverage on machine charm:** If the original PR added behaviour
   to `workload_initialise` (runs under `start`), check whether
   `_on_upgrade_granted` in `upgrade.py` needs the same call.
2. **K8s vs machine parity:** If the original PR touched both `kubernetes/`
   and `machines/`, verify the K8s side doesn't need a separate fix.

## Test Commands
- Unit tests: `PYTHONPATH=src:lib poetry run pytest tests/unit/ -q`
- Lint: `poetry run ruff check src/ tests/`

## Known Divergences Between Branches
- 8.4 `workload_initialise` has an `is_data_dir_initialised()` shortcut
  branch; 8.0 does not.
- 8.4 uses `charmed-stats` as monitoring username; 8.0 uses `monitoring`.

## Files of Interest
- `machines/src/charm.py` — main charm logic, hooks, workload_initialise.
- `machines/src/upgrade.py` — upgrade handling, `_on_upgrade_granted`.
