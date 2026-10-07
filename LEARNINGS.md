# Learnings: Snapcraft + Poetry Plugin

A summary of issues encountered while packing the `bpilot` snap, with
the root cause and resolution for each.

| # | Issue | Root cause | Resolution |
|---|-------|------------|------------|
| 1 | `python3 -m venv` fails: "ensurepip is not available" | core26's `python3.14-venv` declares `Breaks: python3-pip`, which is pre-installed in the build environment. apt silently drops `python3.14-venv` to resolve the conflict, leaving no `ensurepip` module. | Switched from `core26` to `core24` (Python 3.12), where `python3-venv` and `python3-pip` coexist without conflict. |
| 2 | Switching from `python3-venv` to `python3.14-venv` (version-explicit) didn't help | Same `Breaks` relationship applies regardless of whether the generic or version-explicit package is named. apt's conflict resolution is silent — no warning that the package was dropped. | Falling back to `core24` sidesteps the issue entirely. |
| 3 | A manual `override-build` (venv without pip + bootstrap from system wheel) worked but was overly complex | The `Breaks` relationship forced reimplementing the poetry plugin's own build steps (venv creation, pip bootstrap, poetry export, pip install). | Using `core24` lets the poetry plugin run its standard build without intervention. |
| 4 | Build succeeds but post-build fails: "No suitable Python interpreter found in payload" | The poetry plugin's post-build script looks for the interpreter binary (e.g. `python3.12`) in `CRAFT_PART_INSTALL/usr/bin`. We staged `python3.12`, which does NOT contain the binary — only `pdb3.12`, `pydoc3.12`, `pygettext3.12`. The actual `/usr/bin/python3.12` binary lives in `python3.12-minimal`. | Changed stage-packages from `python3.12` to `python3.12-minimal`. |
| 5 | Initial `python3` in stage-packages was a metapackage with no binary | Same as #4 — `python3` is a metapackage that depends on `python3.12` but contains no files itself. | Use `python3.12-minimal` explicitly, which contains the interpreter binary. |
| 6 | `poetry.lock` inconsistency warning during build | The lock file was generated with a different Poetry version than the one the plugin installs. | Non-blocking; regenerate with `poetry lock` if it causes dependency resolution issues. |
