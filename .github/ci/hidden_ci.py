"""CI driver for the ``hidden`` check of the Hive benchmark target.

Runs every task's hidden suite (from the private hidden repository) against
the merge-base and against the candidate head, using the vendored Hive runner
``hidden_pytest_runner.py`` for each run, and fails on:

* any hidden suite that passes on the base but not on the head (regression);
* the PR's own task (from ``hive/delivery/<task_id>-<digest>``) not passing;
* a collected-count mismatch, collection error, timeout or harness error.

Job logs on this repository are PUBLIC.  This script prints only task ids,
outcome reasons and counts.  Pytest output goes to log files in a private
directory that is never printed or uploaded; tracebacks of this script are
suppressed.  Candidate code runs in the child only, under a dedicated
unprivileged uid, without network (loopback only), against read-only trees.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import io

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hidden_pytest_runner as runner  # noqa: E402

DELIVERY_PREFIX = "hive/delivery/"
# Mirrors the Hive delivery executor's default protected paths.
PROTECTED_DIRS = (".github/",)
# pytest.ini at any depth (boltons/pytest.ini) is stricter than the executor's
# default exact `pytest.ini`; CI ignores it anyway (-c /dev/null).
PROTECTED_ANY_DEPTH = ("conftest.py", "pytest.ini")
PROTECTED_EXACT = ("noxfile.py", "pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
_SUFFIX = re.compile(r"[A-Za-z0-9]+")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class Failure(Exception):
    """A gate failure whose message is safe for public logs."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str, cwd: Path, binary: bool = False):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False
    )
    if result.returncode:
        raise Failure(f"git {args[0]} failed")
    return result.stdout if binary else result.stdout.decode()


# ---------------------------------------------------------------- integrity


def load_hidden(hidden_repo: Path) -> dict[str, dict]:
    """Verify every manifest digest; return task_id -> {files, node_ids}."""

    root = hidden_repo / "hidden"
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != "hive.benchmark.hidden-manifest/1":
        raise Failure("integrity: unexpected manifest schema")
    for rel, digest in manifest["files"].items():
        path = root / rel
        if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
            raise Failure(f"integrity: digest mismatch for a file of {rel.split('/')[0]}")
    listed = set(manifest["files"]) | {"manifest.json"}
    for path in root.rglob("*"):
        if path.is_symlink() or (path.is_file() and path.relative_to(root).as_posix() not in listed):
            raise Failure("integrity: unlisted file under hidden/")
    tests = json.loads((root / "hidden-tests-manifest.json").read_text())
    entries = {entry["task_id"]: entry for entry in tests["tasks"]}
    task_ids = manifest["tasks"]
    if sorted(entries) != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
        raise Failure("integrity: hidden-tests manifest and manifest disagree on tasks")
    suites = {}
    for task_id in task_ids:
        entry = entries[task_id]
        task = json.loads((root / task_id / "task.json").read_text())
        files = sorted(entry["files"])
        if (
            task["contract"]["task_id"] != task_id
            or sorted(task["hidden_test_files"]) != files
            or sorted(task["hidden_test_node_ids"]) != sorted(entry["node_ids"])
            or task["hidden_test_id"] != entry["hidden_test_id"]
        ):
            raise Failure(f"integrity: {task_id}: task.json disagrees with hidden-tests manifest")
        for rel, digest in entry["files"].items():
            path = root / task_id / "tests" / rel
            if sha256_file(path) != digest.removeprefix("sha256:"):
                raise Failure(f"integrity: {task_id}: hidden test digest mismatch")
        suites[task_id] = {"files": files, "node_ids": list(entry["node_ids"])}
    print(
        f"integrity ok: {len(task_ids)} tasks; "
        f"manifest.json sha256={sha256_file(root / 'manifest.json')}; "
        f"hidden-tests-manifest.json sha256={sha256_file(root / 'hidden-tests-manifest.json')}",
        flush=True,
    )
    return suites


# ---------------------------------------------------------------- binding


def hive_slug(task_id: str) -> str:
    """Same as hive_delivery.github_merge.delivery_branch_name's slug."""

    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", task_id).strip("-_")
    return (slug or "task")[:48].rstrip("-_") or "task"


def bind_task(head_ref: str, task_ids: list[str]) -> str | None:
    """The task a ``hive/delivery/<slug>-<suffix>`` branch delivers, else None."""

    if not head_ref.startswith(DELIVERY_PREFIX):
        return None
    rest = head_ref[len(DELIVERY_PREFIX):]
    slug, _, suffix = rest.rpartition("-")
    matches = [t for t in task_ids if hive_slug(t) == slug]
    if "/" in rest or not _SUFFIX.fullmatch(suffix) or len(matches) != 1:
        raise Failure("binding: delivery branch does not name exactly one benchmark task")
    return matches[0]


def protected(path: str) -> bool:
    lowered = path.lower()
    name = PurePosixPath(lowered).name
    return (
        any(lowered.startswith(p) or lowered == p.rstrip("/") for p in PROTECTED_DIRS)
        or name in PROTECTED_ANY_DEPTH
        or lowered in PROTECTED_EXACT
    )


# ---------------------------------------------------------------- trees


def export_tree(checkout: Path, commit: str, destination: Path) -> None:
    """``git archive`` of one commit (no .git, nothing untracked), read-only.

    Attributes come from the empty tree, so a committed ``.gitattributes``
    cannot ``export-ignore`` or ``export-subst`` what gets tested.
    """

    data = git("--attr-source=" + EMPTY_TREE, "-c", "core.attributesFile=/dev/null",
               "archive", "--format=tar", commit, cwd=checkout, binary=True)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")
    for directory, dirnames, filenames in os.walk(destination):
        os.chmod(directory, 0o755)
        for name in filenames:
            path = Path(directory, name)
            if not path.is_symlink():
                os.chmod(path, 0o755 if os.access(path, os.X_OK) else 0o644)


def sudo(*args: str) -> None:
    subprocess.run(["sudo", "-n", *args], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hidden", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--sandbox-users", default="",
                        help="comma-separated uids; empty runs unsandboxed (local only)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--tasks", default="", help="local debugging: subset of task ids")
    parser.add_argument("--summary", type=Path)
    options = parser.parse_args()

    suites = load_hidden(options.hidden)
    task_ids = sorted(suites)
    if options.tasks:
        task_ids = [t for t in task_ids if t in options.tasks.split(",")]
    own = bind_task(options.head_ref, sorted(suites))
    print(f"head ref: {options.head_ref or '(push)'}; bound task: {own or 'none'}", flush=True)
    print(f"base {options.base}  head {options.head}", flush=True)

    changed = git("diff", "--name-only", "-z", "--no-renames", options.base, options.head,
                  cwd=options.checkout).split("\0")
    touched = sorted(p for p in changed if p and protected(p))
    if own is not None and touched:
        raise Failure(f"protected paths changed by a delivery branch: {', '.join(touched)}")
    if any(PurePosixPath(p).name.startswith("test_hidden_") for p in changed if p):
        raise Failure("candidate adds or changes a test_hidden_* path")

    work = options.work
    users = [u for u in options.sandbox_users.split(",") if u]
    slots = users or [""] * max(1, options.jobs)
    suites_root = work / "suites"
    results_root = work / "results"
    suites_root.mkdir(parents=True)
    results_root.mkdir()
    os.chmod(work, 0o755)  # pytest scans ancestors of the test paths
    os.chmod(suites_root, 0o711)
    os.chmod(results_root, 0o711)
    options.logs.mkdir(parents=True, exist_ok=True)
    os.chmod(options.logs, 0o700)

    hidden_root = options.hidden / "hidden"
    for task_id in task_ids:
        files = [(rel, (hidden_root / task_id / "tests" / rel).read_bytes())
                 for rel in suites[task_id]["files"]]
        directory = runner.write_suite(suites_root / task_id, files, suites[task_id]["node_ids"])
        for d, _, names in os.walk(directory):
            os.chmod(d, 0o755)
            for name in names:
                os.chmod(Path(d, name), 0o644)

    trees: dict[tuple[int, str], Path] = {}
    for index in range(len(slots)):
        for kind, commit in (("base", options.base), ("head", options.head)):
            path = work / f"w{index}" / kind
            export_tree(options.checkout, commit, path)
            trees[index, kind] = path
        os.chmod(work / f"w{index}", 0o755)

    free: queue.Queue[int] = queue.Queue()
    for index in range(len(slots)):
        free.put(index)

    def run(job: tuple[str, str]) -> tuple[str, str, int, dict | None]:
        task_id, kind = job
        index = free.get()
        user = slots[index]
        tmpdir = work / f"tmp{index}"
        try:
            prefix: list[str] = []
            if user:
                sudo("install", "-d", "-o", user, "-g", user, "-m", "700", str(tmpdir))
                # new network namespace with only loopback up (boltons tests use
                # localhost sockets), then drop to the sandbox uid
                prefix = ["sudo", "-n", "/usr/bin/unshare", "--net", "--",
                          "/bin/sh", "-c", '/usr/sbin/ip link set lo up && exec "$@"', "sh",
                          "/usr/bin/setpriv", f"--reuid={user}", f"--regid={user}",
                          "--clear-groups", "--no-new-privs", "--"]
            else:
                tmpdir.mkdir(mode=0o700)
            code, result = runner.run_suite_counts(
                repository=trees[index, kind],
                suite=suites_root / task_id,
                timeout_seconds=options.timeout,
                python=options.python,
                child_prefix=prefix,
                log_path=options.logs / f"{kind}-{task_id}.log",
                tmpdir=tmpdir,
                scratch_root=results_root,
            )
            return task_id, kind, code, result
        finally:
            if user:
                sudo("pkill", "-KILL", "-u", user)
                sudo("rm", "-rf", "--", str(tmpdir))
            else:
                shutil.rmtree(tmpdir, ignore_errors=True)
            free.put(index)

    jobs = [(t, k) for t in task_ids for k in ("base", "head")]
    outcome: dict[tuple[str, str], tuple[int, dict | None]] = {}
    with cf.ThreadPoolExecutor(len(slots)) as pool:
        for task_id, kind, code, result in pool.map(run, jobs):
            outcome[task_id, kind] = (code, result)

    # The trees must come back exactly as exported (no hidden file left behind).
    for (index, kind), path in trees.items():
        leftovers = [p for p in path.rglob("test_hidden_*")]
        if leftovers:
            raise Failure("harness: hidden files left in a tree copy")

    def show(code: int, result: dict | None) -> str:
        if result is None:
            return runner.exit_reason(code)
        return (f"{runner.exit_reason(code)} {result['passed']}/{result['collected']}"
                + (f" ({result['skipped']} skipped)" if result["skipped"] else ""))

    problems: list[str] = []
    rows = []
    for task_id in task_ids:
        (bcode, bres), (hcode, hres) = outcome[task_id, "base"], outcome[task_id, "head"]
        verdict = "ok"
        if bcode not in (runner.EXIT_PASSED, runner.EXIT_TESTS_FAILED):
            verdict = "BASE-ANOMALY"
            problems.append(f"{task_id}: base {runner.exit_reason(bcode)}")
        if hcode not in (runner.EXIT_PASSED, runner.EXIT_TESTS_FAILED):
            verdict = "HEAD-ANOMALY"
            problems.append(f"{task_id}: head {runner.exit_reason(hcode)}")
        elif bcode == runner.EXIT_PASSED and hcode != runner.EXIT_PASSED:
            verdict = "REGRESSION"
            problems.append(f"{task_id}: passes on base, fails on head")
        if task_id == own and hcode != runner.EXIT_PASSED:
            verdict = "OWN-TASK-FAILS"
            problems.append(f"{task_id}: the PR's own task does not pass on head")
        elif task_id == own:
            verdict = "own task passes"
        rows.append((task_id, show(bcode, bres), show(hcode, hres), verdict))

    width = max(len(t) for t in task_ids)
    lines = [f"{'task_id':<{width}}  {'base':<22}  {'head':<22}  verdict"]
    lines += [f"{t:<{width}}  {b:<22}  {h:<22}  {v}" for t, b, h, v in rows]
    print("\n".join(lines), flush=True)
    base_pass = sum(outcome[t, "base"][0] == 0 for t in task_ids)
    head_pass = sum(outcome[t, "head"][0] == 0 for t in task_ids)
    total = f"hidden suites passing: base {base_pass}/{len(task_ids)}, head {head_pass}/{len(task_ids)}"
    print(total, flush=True)
    if options.summary:
        with open(options.summary, "a") as handle:
            handle.write(f"### hidden\n\nbound task: `{own or 'none'}`; {total}\n\n")
            handle.write("| task | base | head | verdict |\n|---|---|---|---|\n")
            for row in rows:
                handle.write("| " + " | ".join(f"`{row[0]}`" if i == 0 else row[i]
                                                 for i in range(4)) + " |\n")
            handle.write("\n" + ("**FAIL**: " + "; ".join(problems) if problems else "**PASS**") + "\n")
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", flush=True)
        return 1
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as failure:
        print(f"FAIL {failure}", flush=True)
        sys.exit(1)
    except BaseException as error:  # never print a traceback into public logs
        if isinstance(error, SystemExit):
            raise
        print(f"FAIL harness error ({type(error).__name__})", flush=True)
        sys.exit(1)
