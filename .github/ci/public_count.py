"""Fail the public suite when it collected or passed fewer tests than the base.

Reads the JUnit XML written by pytest.  The floors are the fixture base's
counts with this toolchain (no numpy/scipy/pandas/matplotlib installed), so a
candidate cannot pass ``test`` by deleting or skipping public tests.
"""

import argparse
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit")
    parser.add_argument("--min-collected", type=int, required=True)
    parser.add_argument("--min-passed", type=int, required=True)
    options = parser.parse_args()
    root = ET.parse(options.junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {key: sum(int(s.get(key, 0)) for s in suites)
             for key in ("tests", "failures", "errors", "skipped")}
    passed = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    print(f"public suite: collected={total['tests']} passed={passed} "
          f"skipped/xfailed={total['skipped']} failures={total['failures']} errors={total['errors']}")
    problems = []
    if total["tests"] < options.min_collected:
        problems.append(f"collected {total['tests']} < floor {options.min_collected}")
    if passed < options.min_passed:
        problems.append(f"passed {passed} < floor {options.min_passed}")
    if total["failures"] or total["errors"]:
        problems.append("failures or errors present")
    for problem in problems:
        print(f"FAIL {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
