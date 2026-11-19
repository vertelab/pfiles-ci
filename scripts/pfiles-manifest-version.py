#!/usr/bin/env python3
"""pfiles-manifest-version.py -- give the manifest a version that carries the edition.

Odoo reads the series from the version string before the first dot and silently
skips a module whose series does not match the running one. A p-file that says
'version': '1.0' on every edition therefore gives an uncomparable version:
migration scripts cannot match it per edition, and --init/--update behave
differently on 18.0 and 20.0 for no documented reason.

The fix is a small #if-chain in the p-file -- the edition cannot be prefixed
outside preprocess, so it is named there:

    {
        'name': 'M',
        # Version = <Odoo series> + module version. Bump the module version in
        # ALL branches.
        # #if VERSION >= "20.0"
        'version': '20.0.1.0',
        # #elif VERSION == "19.0"
        'version': '19.0.1.0',
        # #else
        'version': '18.0.1.0',
        # #endif
    }

This script rewrites a p-file that has no such chain. It is a migration tool,
NOT a gate: the gate is MANIFEST_VERSION in pfiles-check.py, which fails the
build until the migration has been run.

Usage:
    pfiles-manifest-version.py --repo .            # report what would change
    pfiles-manifest-version.py --repo . --check    # exit 1 if anything needs it
    pfiles-manifest-version.py --repo . --apply    # rewrite the p-files
    pfiles-manifest-version.py --repo . --apply --series 14.0 18.0

Editions: read from .pfiles-ci (branch-editions) or --series. The chain is
written highest series first, so the >= comparison lands right.

Exit:
    0  nothing to migrate (or --apply completed)
    1  something needs migration (--check / report mode)
    2  invalid usage
    5  environment error
"""
from __future__ import annotations

import argparse
import os
import shutil
import re
import subprocess
import sys

# Directory walk exclusions, same set as the other pfiles scripts.
EXCLUDED_DIRS = {".git", "__pycache__", ".tox", ".venv", "venv", "node_modules",
                 ".idea", ".mypy_cache", ".pytest_cache"}

# Matches the version line, keeping its indentation and quote style. Not
# anchored at the line start: a manifest is often written on one line
# ({'name': 'M', 'version': '1.0'}), so the version entry can sit anywhere.
VERSION_LINE_RE = re.compile(
    r"""(?P<indent>[ \t]*)['"]version['"]\s*:\s*(?P<quote>['"])(?P<value>[^'"]*)(?P=quote)""")

# A manifest that already has a version chain must be left alone. Match the
# preprocess directive that mentions VERSION, not the word "version" anywhere.
VERSION_COND_RE = re.compile(r"""^[ \t]*#[ \t]*#[ \t]*(if|elif)\b.*\bVERSION\b""", re.MULTILINE)

# A module version is digits and dots. '1.0', '1.0.1'. Anything with a series
# in it ('17.0.1.0') is migrated by taking the part after the edition.
MODULE_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def pfiles(repo):
    """Every __manifest__.p.py in the repo, as absolute paths."""
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        if "__manifest__.p.py" in files:
            found.append(os.path.join(root, "__manifest__.p.py"))
    return sorted(found)


def read_series(repo):
    """Series from .pfiles-ci ('editions: 18.0 20.0'), or []."""
    path = os.path.join(repo, ".pfiles-ci")
    if not os.path.isfile(path):
        return []
    series = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line.startswith("editions:"):
                series.extend(p for p in line.split(":", 1)[1].replace(",", " ").split() if p)
    return series


def parse_series(values):
    """Validate --series values: '18.0' -- two numbers."""
    out = []
    for value in values:
        if not re.match(r"^\d+\.\d+$", value):
            sys.exit("pfiles-manifest-version: invalid series %r (expected e.g. 18.0)" % value)
        out.append(value)
    return out


def sort_series(series):
    """Highest series first -- the order the #if-chain must be written in."""
    return sorted(set(series), key=lambda s: [int(p) for p in s.split(".")], reverse=True)


def module_version(value, series):
    """The module part of an existing version, or '1.0'.

    '1.0'           -> '1.0'          (no series: the whole thing is the module version)
    '17.0'          -> '1.0'          (a bare series is not a module version)
    '17.0.2.1'      -> '2.1'          (strip a series that is one of ours)
    '18.0.1.0'      -> '1.0'
    """
    value = value.strip()
    if not value:
        return "1.0"
    # A bare series ('17.0') is not a module version -- start from 1.0.
    if value in series or re.match(r"^\d+\.\d+$", value):
        return "1.0"
    # A version whose first two parts name a known series: strip them.
    parts = value.split(".")
    if len(parts) >= 2:
        head = ".".join(parts[:2])
        if head in series:
            remainder = ".".join(parts[2:])
            return remainder or "1.0"
    # Otherwise the whole value is the module version, series prefix or not.
    if MODULE_VERSION_RE.match(value):
        return value
    return "1.0"


def build_chain(indent, quote, modver, series):
    """The #if-chain as text -- one branch per series, highest first.

    A branch per series, not a range: preprocess compares as strings, so
    "9.0" > "10.0". An explicit == for every series cannot be fooled by that,
    and the highest series carries the catch-all #else.
    """
    ordered = sort_series(series)
    lines = ["%s# Version = <Odoo series> + module version. Bump the module version in ALL branches."
             % indent]
    lines.append('%s# #if VERSION == "%s"' % (indent, ordered[0]))
    lines.append("%s'version': %s%s.%s%s," % (indent, quote, ordered[0], modver, quote))
    for edition in ordered[1:]:
        lines.append('%s# #elif VERSION == "%s"' % (indent, edition))
        lines.append("%s'version': %s%s.%s%s," % (indent, quote, edition, modver, quote))
    lines.append("%s# #else" % indent)
    lines.append("%s'version': %s%s.%s%s," % (indent, quote, ordered[0], modver, quote))
    lines.append("%s# #endif" % indent)
    return "\n".join(lines)


def plan(path, series):
    """What would change in one p-file: (old_version, new_block) or None."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if VERSION_COND_RE.search(text):
        return None                       # already migrated -- idempotent
    match = VERSION_LINE_RE.search(text)
    if match is None:
        return None                       # no version line to rewrite
    old = match.group("value")
    modver = module_version(old, series)
    block = build_chain(match.group("indent"), match.group("quote"), modver, series)
    return old, block, match


def rewrite(path, block, match):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new_text = text[:match.start()] + block + text[match.end():]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)


def expand(repo, series):
    """Regenerate the committed output for each series. Returns failures."""
    preprocess = shutil.which("preprocess")
    if preprocess is None:
        print("pfiles-manifest-version: preprocess not in PATH -- the p-files were "
              "rewritten but the output was not regenerated", file=sys.stderr)
        return 1
    failed = 0
    for edition in series:
        for path in pfiles(repo):
            out = path[: -len(".p.py")] + ".py"
            proc = subprocess.run([preprocess, "-f", "-D", "VERSION=" + edition,
                                   "-o", out, path],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print("  ✗ %s (%s): %s" % (os.path.relpath(out, repo), edition,
                                           proc.stderr.strip()[:200]), file=sys.stderr)
                failed += 1
    return failed


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Give the p-file manifest a version that carries the edition.")
    ap.add_argument("--repo", default=".", help="repository to migrate (default: .)")
    ap.add_argument("--series", nargs="*", default=None,
                    help="series to write the chain for (default: from .pfiles-ci)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any p-file needs migration")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the p-files and regenerate the output")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    if not os.path.isdir(repo):
        sys.exit("pfiles-manifest-version: %s is not a directory" % repo)

    series = parse_series(args.series) if args.series else read_series(repo)
    if not series:
        sys.exit("pfiles-manifest-version: no series -- add 'editions: 18.0 20.0' to "
                 ".pfiles-ci or pass --series")
    series = sort_series(series)

    todo = []
    for path in pfiles(repo):
        result = plan(path, series)
        if result is not None:
            todo.append((path, result))

    if not todo:
        print("✓ every p-file manifest already carries a version chain "
              "(%s)" % ", ".join(series))
        return 0

    for path, (old, block, _match) in todo:
        print("  %s  version '%s' ->" % (os.path.relpath(path, repo), old))
        for line in block.splitlines():
            print("      " + line)

    if args.check or not args.apply:
        print("\n%d p-file(s) need migration. Run with --apply to rewrite them."
              % len(todo))
        return 1

    for path, (_old, block, match) in todo:
        rewrite(path, block, match)
    print("\n%d p-file(s) rewritten." % len(todo))

    failures = expand(repo, series)
    if failures:
        print("%d file(s) failed to expand -- fix them before committing" % failures,
              file=sys.stderr)
        return 5 if os.environ.get("PFILES_STRICT") else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
