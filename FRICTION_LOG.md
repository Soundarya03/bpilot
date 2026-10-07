# Friction Log: Packing a Poetry-Based Snap on core26

> **Context:** Building a snap for a Python project that uses Poetry for
> dependency management, targeting the `core26` base with `classic`
> confinement. The expected happy path is simple: declare the `poetry`
> plugin, add the Python packages the base doesn't ship, and let the
> plugin handle the rest. What follows is the actual developer journey.

## Project setup

A straightforward Python CLI tool (`bpilot`) with:

- `pyproject.toml` using `[tool.poetry]` (converted from PEP 621 `[project]`
  so the poetry plugin can run `poetry export`).
- `poetry.lock` present.
- One runtime dependency (`httpx`), three dev dependencies.
- `snapcraft.yaml` with `plugin: poetry`, `base: core26`,
  `confinement: classic`.

Nothing exotic. The documentation for the
[poetry plugin](https://documentation.ubuntu.com/snapcraft/9/reference/plugins/poetry_plugin/)
describes five steps the plugin performs during build:

1. Create a virtualenv in `CRAFT_PART_INSTALL`.
2. `poetry export` → `requirements.txt`.
3. `pip install` dependencies.
4. `pip install` the source package.
5. `pip check`.

The expectation is that once the right build/stage packages are declared,
the plugin handles all of this. In practice, step 1 alone required three
iterations to get past.

## User journey

### Attempt 1: Trust the plugin, add Python

**Belief:** The poetry plugin knows how to create a venv; core26 doesn't
ship Python, so I just need to add the interpreter and pip as build
packages, and Python + git as stage packages.

```yaml
parts:
  bpilot:
    plugin: poetry
    source: .
    build-packages:
      - python3
      - python3-venv
      - python3-pip
    stage-packages:
      - python3
      - git
```

**Result:** `snapcraft pack` fails.

```
+ python3 -m venv /root/parts/bpilot/install
The virtual environment was not created successfully because ensurepip is not
available.  On Debian/Ubuntu systems, you need to install the python3-venv
package using the following command.
    apt install python3.14-venv
```

**Friction point:** The error message names `python3.14-venv`, not
`python3-venv` (which was already listed). It's not obvious why the
package I declared didn't install. The user has to stop packing and
investigate Debian package relationships — knowledge that the plugin
should arguably shield them from.

### Attempt 2: Name the version-explicit package

**Diagnosis:** core26 is based on Ubuntu 26.04, which ships Python 3.14.
The generic `python3-venv` meta-package should depend on
`python3.14-venv`, but perhaps it doesn't reliably pull it in inside the
build environment. Let me be explicit.

```yaml
    build-packages:
      - python3.14
      - python3.14-venv
```

**Result:** Same failure. Identical error message, still asking for
`python3.14-venv`.

**Friction point:** The package I explicitly named is the one apt
claims is missing. There's no indication in the snapcraft output of
*why* it's missing — apt's conflict resolution happens silently during
the pull step, so the build step fails with no breadcrumb back to the
real cause. The user now has to reason about apt internals.

### Attempt 3: Work around the apt conflict (custom build script)

**Diagnosis (finally):** `python3.14-venv` declares
`Breaks: python3-pip`. The core26 build environment pre-installs
`python3-pip`, so apt resolves the conflict by **silently dropping
`python3.14-venv`** — leaving no `ensurepip` module, which
`python3 -m venv` needs to install pip into the venv.

The workaround: create the venv without pip, bootstrap pip from the
system wheel, then run the poetry plugin's steps manually.

```yaml
    build-packages:
      - python3.14
      - python3-pip-whl
      - python3-setuptools-whl
    override-build: |
      python3 -m venv --without-pip ${CRAFT_PART_INSTALL}
      PIP_WHL=$(ls /usr/share/python-wheels/pip-*.whl | head -1)
      ${CRAFT_PART_INSTALL}/bin/python3 "${PIP_WHL}/pip" install \
        --no-index --find-links /usr/share/python-wheels pip setuptools
      ${CRAFT_PART_INSTALL}/bin/pip install poetry
      cd ${CRAFT_PART_BUILD}
      ${CRAFT_PART_INSTALL}/bin/poetry export -f requirements.txt -o requirements.txt
      ${CRAFT_PART_INSTALL}/bin/pip install -r requirements.txt
      ${CRAFT_PART_INSTALL}/bin/pip install . --no-deps
      ${CRAFT_PART_INSTALL}/bin/pip check
```

**Result:** This would work, but it defeats the entire purpose of using
the poetry plugin — I'm now manually reimplementing the five steps the
plugin was supposed to handle. At this point the plugin is providing
no value over `plugin: nil` with a hand-written build script.

**Friction point:** The user has been forced to become an expert in:
- Debian package `Breaks` semantics
- The distinction between `python3-pip` (the system package) and
  `python3-pip-whl` (the wheel file)
- Where pip wheels live on Ubuntu (`/usr/share/python-wheels/`)
- The poetry plugin's internal build sequence (to replicate it)

None of this is in the plugin documentation. The documentation says
core26 needs Python staged, but says nothing about the venv/pip
conflict that prevents the plugin from running.

### Attempt 4: Fall back to core24

**Decision:** core24 (Ubuntu 24.04, Python 3.12) doesn't have the
`python3.14-venv` / `python3-pip` `Breaks` relationship. The poetry
plugin works as documented on core24 with no `override-build`.

```yaml
base: core24
# ...
    build-packages:
      - python3
      - python3-venv
```

**Result:** (presumably works — core24's `python3-venv` and
`python3-pip` coexist cleanly.)

**Friction point:** The user has to abandon the current LTS base and
drop to an older one to use the plugin as documented. This is a
workaround, not a fix — and it's not discoverable without having gone
through the three failed attempts above.

### Attempt 5: Interpreter not found in payload (core24)

After switching to core24 and getting the build to succeed, the plugin's
post-build step failed:

```
Looking for a Python interpreter called "python3.12" in the payload...
Python interpreter not found in payload.
No suitable Python interpreter found, giving up.
```

**Diagnosis:** The plugin creates a venv whose `python3` symlink points
to the system interpreter (e.g. `/usr/bin/python3.12`). After building,
it looks for a `python3.12` binary in `CRAFT_PART_INSTALL/usr/bin` (from
stage-packages) so it can repoint the venv at the bundled interpreter.
We had `python3.12` in stage-packages — but the `python3.12` apt package
does **not** contain the interpreter binary. It only ships `pdb3.12`,
`pydoc3.12`, and `pygettext3.12`. The actual `/usr/bin/python3.12`
binary lives in `python3.12-minimal`.

**Resolution:** Changed stage-packages from `python3.12` to
`python3.12-minimal`. The snap then packed successfully.

**Friction point:** This is a Debian packaging detail that is not
obvious and not documented in the plugin's docs. The `python3.12`
package *sounds* like it should contain Python 3.12 — its name gives no
hint that it's a supplementary package of tools. The plugin's post-build
error message ("Python interpreter not found in payload") doesn't
suggest which package to stage, even though the plugin knows the exact
binary name it's looking for (`python3.12`).

### Attempt 6: Working snap on core24

```yaml
base: core24
# ...
    stage-packages:
      - python3.12-minimal  # not python3.12 — that's just tools
      - git
```

**Result:** Snap packs successfully.

**Friction point:** It took five iterations to arrive at a working
`snapcraft.yaml` for a one-dependency Python project. The final config
contains a comment explaining *why* `python3.12-minimal` and not
`python3.12`, because the choice is non-obvious and the next person to
edit the file would otherwise make the same mistake.

## Summary of friction

| # | Issue | Impact on user |
|---|-------|----------------|
| 1 | `python3.14-venv` `Breaks: python3-pip`, and core26's build env pre-installs `python3-pip`, so apt silently drops the venv package | The poetry plugin can't create a venv; the plugin's first build step fails |
| 2 | The failure surfaces as "ensurepip is not available" with no link back to the apt conflict that caused it | The user has to independently diagnose Debian package relationships |
| 3 | The poetry plugin documentation doesn't mention this conflict or how to work around it | The user has no guided path to a working snap on core26 |
| 4 | The working workaround (manual `override-build`) requires reimplementing the plugin's own build steps | The plugin provides no value when its work has to be redone manually |
| 5 | `python3.12` stage-package doesn't contain the interpreter binary; `python3.12-minimal` does | The user has to know Debian's `python3.X` vs `python3.X-minimal` split to stage the right package |
| 6 | The plugin's "interpreter not found in payload" error doesn't suggest which package provides the missing binary | The user has to independently discover `python3.12-minimal` via `dpkg -L` |

## Ideal experience

The poetry plugin should handle Python venv creation and interpreter
bundling transparently, regardless of base. Specifically:

1. **On core26:** If the `python3.14-venv` / `python3-pip` `Breaks`
   conflict is known, the plugin should either resolve it internally
   (e.g., create the venv with `--without-pip` and bootstrap pip from
   the system wheel, so the `Breaks` relationship never matters) or
   document the specific build-packages combination that avoids it.

2. **On all bases with classic confinement:** The plugin's post-build
   step knows the exact interpreter binary name it's looking for (e.g.,
   `python3.12`). When it's not found in the payload, the error message
   should name the apt package that provides it — e.g., "Stage
   `python3.12-minimal` to provide the `python3.12` interpreter." The
   current message ("No suitable Python interpreter found, giving up")
   gives the user no actionable direction.

3. **In the plugin documentation:** The "Dependencies" section should
   clarify that for classic confinement, the interpreter must be staged
   via `python3.X-minimal` (which contains the binary), not
   `python3.X` (which contains supplementary tools only). This is a
   common trap because the package names don't communicate their
   contents.

Either way, a user with a valid `pyproject.toml` and `poetry.lock`
should be able to get a working snap with:

```yaml
parts:
  my-app:
    plugin: poetry
    source: .
    build-packages:
      - python3    # only because core24/26 doesn't ship it
    stage-packages:
      - python3    # the plugin should resolve this to the right package
```

…without having to understand Debian packaging internals, `Breaks`
relationships, or the `python3.X` vs `python3.X-minimal` split.
