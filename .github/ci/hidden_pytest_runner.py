"""Run one task's hidden pytest suite under an explicit, repo-blind config.

Vendored from Hive ``src/hive_benchmark/hidden_pytest_runner.py`` (phase1 @
1cce2ae9).  The exit codes, placement, ``-c /dev/null``/``--rootdir``/
``-p no:cacheprovider`` invocation, nonce and counting are unchanged.  CI-only
additions (see ``.github/ci/hidden_ci.py``): ``run_suite_counts`` also returns
the counters; the child may be started under a sandbox prefix (another uid,
no network), with its pytest output sent to a private log file instead of
``/dev/null``, a private ``TMPDIR``/``HOME``, and a result directory that the
sandbox uid can write.  Without those options it behaves exactly as Hive's.

This file is uploaded verbatim to verifier VMs and executed by the hidden
test interpreter, so it imports nothing but the standard library (and, in
the child process only, ``pytest``).  The verification guest only names a
catalogued test ID and records the command's exit code, so this runner turns
"the hidden suite passed" into exactly one exit code:

``0`` passed
    pytest exited ``0``, collected exactly the declared items, and every one
    of them passed (no failure, error, skip, or xfail).
``1`` tests failed
    the declared items were collected but at least one did not pass.
``2`` wrong item count
    pytest collected zero items, or a count different from the suite's
    declared ``node_ids``.  A suite that collects nothing can never pass.
``3`` harness error
    the suite could not be placed (a hidden test path already exists in the
    candidate tree), the child died or exited early (``os._exit`` at import
    time), or it wrote no (or a forged) result.
``4`` timeout
``5`` collection error
    a hidden test module (or a parametrization) failed to import or
    collect, usually because the candidate broke an import the tests need.

The suite lives outside the repository (``<suite>/<repo path>`` plus
``hive-suite.json``).  Hidden test files sit next to the public test module
they were derived from, so the runner copies each into the candidate tree at
its repository path, runs exactly the declared node IDs from the repository
root, and removes every copied file and created directory afterwards.

The candidate's repository config never changes how the suite runs: pytest
gets ``-c /dev/null`` (``pytest.ini``/``pyproject.toml``/``setup.cfg``/
``tox.ini`` are ignored), an explicit ``--rootdir``, ``-p no:cacheprovider``
(nothing is written into the tree), plugin autoloading disabled, and an
allowlisted environment without ``PYTEST_ADDOPTS``/``PYTHONPATH``.  Both
processes run with ``-I``; the child imports ``pytest`` before the
repository is put on ``sys.path``.

The repository's ``conftest.py`` files still load, as they do for its public
suite: hidden tests derived from public test modules rely on their fixtures,
so ``--noconftest`` would break them.  They are the *base's* conftests,
because the controller refuses, before any verifier runs, a candidate whose
patch changes any path outside its task's write set (computed from the
durable patch, not the author's receipt).  So a candidate cannot plant a
``conftest.py``, pytest configuration, ``sitecustomize.py``, or ``.pth``
file unless the task's own write set names it.

The child receives a per-run nonce on stdin and must echo it in its result
file, so a candidate that exits ``0`` early or writes a result of its own is
caught.  Like the snapshot leak guard, this is a tripwire against workaround
hunting, not a sandbox against a determined adversary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Sequence


SUITE_SCHEMA = "hive-hidden-suite-v1"
SUITE_MANIFEST = "hive-suite.json"
EXIT_PASSED = 0
EXIT_TESTS_FAILED = 1
EXIT_COUNT_MISMATCH = 2
EXIT_HARNESS_ERROR = 3
EXIT_TIMEOUT = 4
EXIT_COLLECTION_ERROR = 5
EXIT_REASONS = {
    EXIT_PASSED: "passed",
    EXIT_TESTS_FAILED: "tests_failed",
    EXIT_COUNT_MISMATCH: "collected_count_mismatch",
    EXIT_HARNESS_ERROR: "harness_error",
    EXIT_TIMEOUT: "timeout",
    EXIT_COLLECTION_ERROR: "collection_error",
}
DEFAULT_TIMEOUT_SECONDS = 25 * 60
_ENVIRONMENT = ("HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_FILE", "TMPDIR")
_COUNTERS = (
    "collected",
    "passed",
    "failed",
    "errors",
    "skipped",
    "xfailed",
    "collection_errors",
)


def exit_reason(code: object) -> str:
    """A stable, reportable reason for a runner exit code."""

    if isinstance(code, int) and not isinstance(code, bool):
        return EXIT_REASONS.get(code, "unexpected_exit_code")
    return "unknown"


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("suite paths must be relative POSIX paths")
    parts = PurePosixPath(value).parts
    if (
        value.startswith("/")
        or value != str(PurePosixPath(*parts))
        or any(part in {"", ".", ".."} for part in parts)
        or any(ord(character) < 32 for character in value)
        or value == SUITE_MANIFEST
    ):
        raise ValueError("suite paths must be relative POSIX paths")
    return value


# --------------------------------------------------------------------------
# Controller side: what is uploaded and catalogued for one suite.


def suite_manifest_bytes(files: Sequence[str], node_ids: Sequence[str]) -> bytes:
    """The ``hive-suite.json`` placed at the suite root on the verifier."""

    return (
        json.dumps(
            {
                "schema_version": SUITE_SCHEMA,
                "files": [_relative(path) for path in files],
                "node_ids": list(node_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def write_suite(
    directory: Path, files: Sequence[tuple[str, bytes]], node_ids: Sequence[str]
) -> Path:
    """Materialize a suite (files plus ``hive-suite.json``) for a local run."""

    directory.mkdir(parents=True, exist_ok=True)
    for path, body in files:
        target = directory / _relative(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (directory / SUITE_MANIFEST).write_bytes(
        suite_manifest_bytes([path for path, _ in files], node_ids)
    )
    return directory


def runner_source() -> bytes:
    """This file's bytes, uploaded to the verifier as the runner."""

    return Path(__file__).read_bytes()


def catalog_command(
    *,
    interpreter: str,
    runner: str,
    repository: str,
    suite: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """The verification-catalog argv that runs one suite."""

    for value in (interpreter, runner, repository, suite):
        if not value.startswith("/"):
            raise ValueError("hidden suite command paths must be absolute")
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    return [
        interpreter,
        "-I",
        runner,
        "--repository",
        repository,
        "--suite",
        suite,
        "--timeout",
        str(timeout_seconds),
    ]


# --------------------------------------------------------------------------
# Parent: place the suite, spawn the child, judge its result, clean up.


def load_suite(suite: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(files, node_ids)`` from ``<suite>/hive-suite.json``."""

    value = json.loads((suite / SUITE_MANIFEST).read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "files", "node_ids"}
        or value["schema_version"] != SUITE_SCHEMA
    ):
        raise ValueError("hidden suite manifest is invalid")
    files = value["files"]
    node_ids = value["node_ids"]
    for items in (files, node_ids):
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item for item in items)
            or len(set(items)) != len(items)
        ):
            raise ValueError("hidden suite manifest is invalid")
    files = [_relative(path) for path in files]
    if any(node_id.split("::", 1)[0] not in files for node_id in node_ids):
        raise ValueError("hidden suite item names a file outside the suite")
    return tuple(files), tuple(node_ids)


def _child_environment(tmpdir: Path | None = None) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _ENVIRONMENT if os.environ.get(name)
    }
    if tmpdir is not None:
        environment.update(TMPDIR=str(tmpdir), HOME=str(tmpdir))
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return environment


def judge(result: dict[str, Any], *, expected: int) -> int:
    """Map a child result onto the runner's exit code."""

    if result.get("collection_errors"):
        return EXIT_COLLECTION_ERROR
    if result["collected"] != expected or expected < 1:
        return EXIT_COUNT_MISMATCH
    if (
        result["exit_code"] != 0
        or result["passed"] != expected
        or result["failed"]
        or result["errors"]
        or result["skipped"]
        or result["xfailed"]
    ):
        return EXIT_TESTS_FAILED
    return EXIT_PASSED


def _read_result(path: Path, nonce: str) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"nonce", "exit_code", *_COUNTERS}
        or value["nonce"] != nonce
        or any(
            not isinstance(value[name], int) or isinstance(value[name], bool)
            for name in ("exit_code", *_COUNTERS)
        )
    ):
        return None
    return value


def _place(repository: Path, suite: Path, files: Sequence[str]) -> list[Path]:
    """Copy the suite into the tree; return what to remove, deepest first."""

    created: list[Path] = []
    try:
        for path in files:
            target = repository / path
            if target.exists() or target.is_symlink():
                raise FileExistsError(path)
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            if not parent.resolve().is_relative_to(repository):
                raise ValueError("hidden test path escapes the repository")
            for directory in reversed(missing):
                directory.mkdir()
                created.append(directory)
            shutil.copyfile(suite / path, target)
            created.append(target)
    except BaseException:
        _remove(created)
        raise
    return created


def _remove(created: Sequence[Path]) -> None:
    for item in reversed(created):
        try:
            if item.is_dir() and not item.is_symlink():
                item.rmdir()
            else:
                item.unlink()
        except OSError:
            pass


def run_suite(
    *,
    repository: Path,
    suite: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
) -> int:
    """Run the suite at ``suite`` against ``repository``; return the exit code."""

    code, _ = run_suite_counts(
        repository=repository,
        suite=suite,
        timeout_seconds=timeout_seconds,
        python=python,
    )
    return code


def run_suite_counts(
    *,
    repository: Path,
    suite: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    python: str | None = None,
    child_prefix: Sequence[str] = (),
    log_path: Path | None = None,
    tmpdir: Path | None = None,
    scratch_root: Path | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Like ``run_suite`` but also return the child's counters (or ``None``).

    ``child_prefix`` (for example ``sudo -n unshare --net setpriv --reuid ...``)
    is prepended to the child argv; because such wrappers reset the
    environment, the allowlisted environment is then passed through
    ``env -i``.  ``log_path`` receives the child's stdout and stderr (pytest
    output) instead of ``/dev/null``; it must not be published.
    """

    try:
        repository = repository.resolve(strict=True)
        suite = suite.resolve(strict=True)
        files, node_ids = load_suite(suite)
        created = _place(repository, suite, files)
    except (OSError, ValueError):
        return EXIT_HARNESS_ERROR, None
    nonce = secrets.token_hex(16)
    environment = _child_environment(tmpdir)
    try:
        with tempfile.TemporaryDirectory(
            prefix="hive-hidden-result-", dir=scratch_root
        ) as scratch:
            if child_prefix:
                # the sandbox uid may create (not list) the result file here
                os.chmod(scratch, 0o733)
            result_path = Path(scratch) / "result.json"
            argv = [
                python or sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--child",
                "--repository",
                str(repository),
                "--suite",
                str(suite),
                "--result",
                str(result_path),
            ]
            if child_prefix:
                argv = [
                    *child_prefix,
                    "/usr/bin/env",
                    "-i",
                    *(f"{name}={value}" for name, value in environment.items()),
                    *argv,
                ]
            sink = open(log_path, "ab") if log_path is not None else None
            try:
                subprocess.run(
                    argv,
                    cwd=repository,
                    env=environment,
                    input=nonce.encode(),
                    stdout=sink if sink is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if sink is not None else subprocess.DEVNULL,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return EXIT_TIMEOUT, None
            finally:
                if sink is not None:
                    sink.close()
            result = _read_result(result_path, nonce)
    finally:
        _remove(created)
    if result is None:
        return EXIT_HARNESS_ERROR, None
    return judge(result, expected=len(node_ids)), result


# --------------------------------------------------------------------------
# Child: run pytest in-process with a counting plugin.


class _Counter:
    def __init__(self) -> None:
        self.collected = 0
        self.passed = 0
        self.failed = 0
        self.errors = 0
        self.skipped = 0
        self.xfailed = 0
        self.collection_errors = 0

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report: Any) -> None:
        xfail = hasattr(report, "wasxfail")
        if report.when == "call":
            if xfail:
                self.xfailed += 1
            elif report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
        elif report.failed:
            self.errors += 1
        elif report.skipped:
            if xfail:
                self.xfailed += 1
            else:
                self.skipped += 1


def _child(repository: Path, suite: Path, result: Path) -> int:
    nonce = sys.stdin.read().strip()
    sys.dont_write_bytecode = True
    import pytest  # imported before the repository can shadow it

    _, node_ids = load_suite(suite)
    counter = _Counter()
    sys.path.insert(0, str(repository))
    code = pytest.main(
        [
            "-c",
            os.devnull,
            "--rootdir",
            str(repository),
            "-p",
            "no:cacheprovider",
            "-q",
            *node_ids,
        ],
        plugins=[counter],
    )
    body = json.dumps(
        {
            "nonce": nonce,
            "exit_code": int(code),
            **{name: getattr(counter, name) for name in _COUNTERS},
        }
    ).encode()
    descriptor = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one hidden pytest suite.")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--result", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.child:
        if options.result is None:
            return EXIT_HARNESS_ERROR
        return _child(options.repository, options.suite, options.result)
    return run_suite(
        repository=options.repository,
        suite=options.suite,
        timeout_seconds=options.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
