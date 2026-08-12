"""Tests for bpilot.validator — static checks on resolved files."""

from __future__ import annotations

from pathlib import Path

from bpilot.validator import (
    validate_changes,
    validate_file,
    validate_files,
)


def test_valid_python_file_passes(tmp_path: Path):
    path = tmp_path / "good.py"
    path.write_text("x = 1\nprint(x)\n")
    result = validate_file("good.py", cwd=tmp_path)
    assert result.ok is True
    assert result.errors == []


def test_conflict_markers_fail(tmp_path: Path):
    path = tmp_path / "conflict.py"
    path.write_text("<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> branch\n")
    result = validate_file("conflict.py", cwd=tmp_path)
    assert result.ok is False
    assert any("conflict markers" in e for e in result.errors)


def test_syntax_error_fails(tmp_path: Path):
    path = tmp_path / "bad.py"
    path.write_text("def f(\n")
    result = validate_file("bad.py", cwd=tmp_path)
    assert result.ok is False
    assert any("syntax" in e.lower() for e in result.errors)


def test_placeholder_todo_fails(tmp_path: Path):
    path = tmp_path / "todo.py"
    path.write_text("# TODO: fix this later\nx = 1\n")
    result = validate_file("todo.py", cwd=tmp_path)
    assert result.ok is False
    assert any("TODO" in e for e in result.errors)


def test_placeholder_fixme_fails(tmp_path: Path):
    path = tmp_path / "fixme.py"
    path.write_text("# FIXME: broken\nx = 1\n")
    result = validate_file("fixme.py", cwd=tmp_path)
    assert result.ok is False
    assert any("FIXME" in e for e in result.errors)


def test_non_python_file_skips_syntax_check(tmp_path: Path):
    path = tmp_path / "config.md"
    path.write_text("# Some config\n\nThis is fine.\n")
    result = validate_file("config.md", cwd=tmp_path)
    assert result.ok is True


def test_missing_file_fails(tmp_path: Path):
    result = validate_file("nonexistent.py", cwd=tmp_path)
    assert result.ok is False
    assert any("not found" in e for e in result.errors)


def test_validate_files_returns_one_per_path(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("def f(\n")
    results = validate_files(["a.py", "b.py"], cwd=tmp_path)
    assert len(results) == 2
    assert results[0].ok is True
    assert results[1].ok is False


def test_valid_complex_python_file_passes(tmp_path: Path):
    path = tmp_path / "complex.py"
    path.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "def main() -> int:\n"
        "    p = Path('.')\n"
        "    for f in p.iterdir():\n"
        "        print(f)\n"
        "    return 0\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )
    result = validate_file("complex.py", cwd=tmp_path)
    assert result.ok is True


def test_validate_changes_passes_clean_files(tmp_path: Path):
    """validate_changes runs per-file checks on all changed files."""
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "good2.py").write_text("y = 2\n")
    result = validate_changes(changed_files=["good.py", "good2.py"], cwd=tmp_path)
    assert result.ok is True
    assert len(result.file_results) == 2


def test_validate_changes_detects_conflict_markers(tmp_path: Path):
    """validate_changes catches conflict markers in changed files."""
    (tmp_path / "bad.py").write_text("<<<<<<< HEAD\nx = 1\n=======\n>>>>>>> branch\n")
    result = validate_changes(changed_files=["bad.py"], cwd=tmp_path)
    assert result.ok is False
    assert "bad.py" in result.failed_files


def test_validate_changes_skips_lock_regen_when_no_dependency_change(tmp_path: Path):
    """No lock regeneration when dependency files weren't changed."""
    (tmp_path / "app.py").write_text("x = 1\n")
    result = validate_changes(changed_files=["app.py"], cwd=tmp_path)
    assert result.lock_regen == []


def test_validate_changes_no_lint_without_pyproject(tmp_path: Path):
    """No lint commands run when there's no pyproject.toml."""
    (tmp_path / "app.py").write_text("x = 1\n")
    result = validate_changes(changed_files=["app.py"], cwd=tmp_path)
    assert result.lint_results == []
