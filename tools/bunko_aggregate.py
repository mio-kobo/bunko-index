#!/usr/bin/env python3
"""Decide the final "Package tests" result from the upstream job results.

A skipped matrix job is acceptable only when planning explicitly reported that
there are no new package records. Anything unexpected fails.
"""

import argparse
import sys


def verdict(plan, test, tooling, has_packages):
    """Return (passed, reason) for GitHub `needs.<job>.result` values and the plan output."""
    if plan != "success":
        return False, f"planning did not succeed (result: {plan or 'missing'})"
    if tooling != "success":
        return False, f"tooling tests did not succeed (result: {tooling or 'missing'})"
    if has_packages == "true":
        if test == "success":
            return True, "every package test job passed"
        return False, f"new package records require tests, but the test jobs finished as: {test or 'missing'}"
    if has_packages == "false":
        if test == "skipped":
            return True, "no new package records; package tests were correctly skipped"
        return False, f"plan reported no package records, but the test jobs finished as: {test or 'missing'}"
    return False, f"plan output has_packages is {has_packages!r}, expected 'true' or 'false'"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "test", "tooling", "has-packages"):
        parser.add_argument(f"--{name}", default="")
    args = parser.parse_args(argv)
    passed, reason = verdict(args.plan, args.test, args.tooling, args.has_packages)
    print(f"{'PASS' if passed else 'FAIL'}: {reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
