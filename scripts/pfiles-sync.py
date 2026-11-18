#!/usr/bin/env python3
"""pfiles-sync.py -- keep the SAME p-files on every edition branch.

The principle: a p-file is the source for all editions. Every p-file must
therefore exist on every edition branch with identical content -- edition
differences are expressed with '# #if VERSION' inside the file, never by the
file living on only one branch.

Two modes:

  --check   report drift: p-files missing on a branch, or present on several
            branches with different content. Exit 1 on drift.
  --apply   repair the drift: copy each p-file to the branches that lack it,
            and update the ones holding older content.

Which branch is the source when content differs cannot be decided
automatically -- that is a human judgement. --apply therefore requires
--from, and copies from that branch to the others.

Usage:
    pfiles-sync.py --repo . --check
    pfiles-sync.py --repo . --check --json
    pfiles-sync.py --repo . --apply --from 18.0 --push

Exit codes:
    0  no drift (--check) / everything in sync (--apply)
    1  drift found (--check)
    2  invalid usage
    5  environment error (git missing, branch absent)
"""

import argparse
import json
import os
import subprocess
import sys

# Extensions treated as p-files. Same list as the hook and the checker.
EXTENSIONS = (".py", ".xml", ".js", ".csv", ".sh", ".html", ".htm")


def die(msg, code=5):
    print("pfiles-sync: %s" % msg, file=sys.stderr)
    sys.exit(code)


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        die("git %s failed: %s" % (" ".join(args), proc.stderr.strip()[:300]))
    return proc


def edition_branches(repo, wanted):
    """Those of 'wanted' that exist as a branch (local or origin/)."""
    out = git(repo, "for-each-ref", "--format=%(refname:short)",
              "refs/heads", "refs/remotes").stdout
    have = {line.strip().rsplit("/", 1)[-1] for line in out.splitlines() if line.strip()}
    return [e for e in wanted if e in have]


def ref(repo, branch):
    """The ref to read: origin/<branch> when it exists, else the local branch.

    CI always has origin/. A local repo may not, and reading the local branch
    then is the same question asked of the same tree.
    """
    if git(repo, "rev-parse", "--verify", "--quiet", "origin/%s" % branch,
           check=False).returncode == 0:
        return "origin/%s" % branch
    return branch


def pfiles_on(repo, branch):
    """P-files present on the branch, as relative paths."""
    out = git(repo, "ls-tree", "-r", "--name-only", ref(repo, branch)).stdout
    return {line.strip() for line in out.splitlines()
            if line.strip().endswith(tuple(".p" + e for e in EXTENSIONS))}


def blob(repo, branch, path):
    """The file's content on the branch, or None if absent."""
    proc = git(repo, "show", "%s:%s" % (ref(repo, branch), path), check=False)
    return proc.stdout if proc.returncode == 0 else None


def check(repo, branches):
    """Return {'missing': {branch: [files]}, 'diverged': [{'path', 'branches'}]}."""
    per_branch = {b: pfiles_on(repo, b) for b in branches}
    union = set()
    for files in per_branch.values():
        union |= files

    missing = {}
    for b in branches:
        absent = sorted(union - per_branch[b])
        if absent:
            missing[b] = absent

    diverged = []
    for path in sorted(union):
        contents = {}
        for b in branches:
            if path in per_branch[b]:
                contents[b] = blob(repo, b, path)
        if len(set(contents.values())) > 1:
            diverged.append({"path": path, "branches": sorted(contents)})

    return {"missing": missing, "diverged": diverged,
            "editions": branches,
            "counts": {b: len(per_branch[b]) for b in branches}}


def apply(repo, branches, source, push):
    """Copy every p-file from 'source' to the other branches and expand there."""
    if source not in branches:
        die("source branch %s does not exist (have: %s)"
            % (source, ", ".join(branches)), 2)

    # The union of all p-files, not just the source's: a file present on only
    # ONE branch is drift too and must reach the others. The source decides
    # the content when the file exists on several branches.
    src_files = set()
    for b in branches:
        src_files |= pfiles_on(repo, b)
    changed = {}

    for branch in branches:
        # The source branch is not skipped: it may lack a file that only
        # exists on another branch, and then it should get it too.
        theirs = pfiles_on(repo, branch)
        todo = []
        wanted = {}
        for path in sorted(src_files):
            want = blob(repo, source, path)
            if want is None:
                # The file exists only on branches other than the source.
                # Then the branch that HAS the file is the source for that
                # particular file -- a unique file is spread, not ignored.
                for other in branches:
                    if other == branch:
                        continue
                    cand = blob(repo, other, path)
                    if cand is not None:
                        want = cand
                        break
            if want is None:
                continue
            have = blob(repo, branch, path) if path in theirs else None
            if have != want:
                todo.append(path)
                wanted[path] = want
        if not todo:
            continue

        # Work in a temporary worktree so the checked-out branch is untouched.
        work = os.path.join(repo, ".git", "pfiles-sync-%s" % branch)
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", work],
                       capture_output=True)
        git(repo, "worktree", "add", "-f", "--detach", work, "origin/%s" % branch)

        for path in todo:
            content = wanted[path]
            dest = os.path.join(work, path)
            os.makedirs(os.path.dirname(dest) or work, exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)

        # Expand for THIS edition and put the generated code in the tree.
        # The expansion writes to a directory outside the tree, so nothing
        # half-finished ends up in the commit.
        expand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "pfiles-expand.py")
        outroot = os.path.join(repo, ".git", "pfiles-sync-expand-%s" % branch)
        subprocess.run(["rm", "-rf", outroot], capture_output=True)
        copied = 0
        if os.path.exists(expand):
            subprocess.run([sys.executable, expand, "--repo", work,
                            "--version", branch, "--out", outroot],
                           capture_output=True, text=True)
            for root, _dirs, files in os.walk(outroot):
                for name in files:
                    srcf = os.path.join(root, name)
                    rel = os.path.relpath(srcf, outroot)
                    if rel.endswith(tuple(".p" + e for e in EXTENSIONS)):
                        continue        # the p-files are already copied
                    dst = os.path.join(work, rel)
                    os.makedirs(os.path.dirname(dst) or work, exist_ok=True)
                    with open(srcf, "rb") as a, open(dst, "wb") as b:
                        b.write(a.read())
                    copied += 1
            subprocess.run(["rm", "-rf", outroot], capture_output=True)
        print("pfiles-sync: %s -- %d p-files, %d generated"
              % (branch, len(todo), copied), file=sys.stderr)

        git(work, "add", "-A")
        msg = ("pfiles: sync from %s\n\n"
               "The same p-files must exist on every edition branch; edition\n"
               "differences are expressed with '# #if VERSION' inside the file.\n"
               "The generated files are expanded for %s." % (source, branch))
        proc = git(work, "-c", "user.email=ci@vertel.se",
                   "-c", "user.name=pfiles-sync", "commit", "-q", "-m", msg,
                   check=False)
        if proc.returncode != 0:
            changed[branch] = "nothing to commit"
        else:
            sha = git(work, "rev-parse", "--short", "HEAD").stdout.strip()
            changed[branch] = "%s (%d files)" % (sha, len(todo))
            # The commit lives on a detached HEAD inside a temporary worktree
            # that is removed below. Without moving the branch, the commit is
            # unreachable and is garbage-collected -- the sync reports success
            # and changes nothing. Always advance the local branch.
            full = git(work, "rev-parse", "HEAD").stdout.strip()
            mv = git(repo, "branch", "-f", branch, full, check=False)
            if mv.returncode != 0:
                changed[branch] += " BRANCH NOT MOVED"
            if push:
                # Push the branch we just moved, not HEAD: HEAD lives in a
                # temporary worktree that was checked out from origin/<branch>
                # and is removed below. Pushing HEAD from there sent the OLD
                # commit and left the sync commit unreachable.
                p = git(repo, "push", "origin", "%s:refs/heads/%s" % (branch, branch),
                        check=False)
                changed[branch] += " pushed" if p.returncode == 0 else " PUSH FAILED"
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", work],
                       capture_output=True)

    return changed


def main():
    ap = argparse.ArgumentParser(description="Sync p-files between edition branches.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--editions", default="18.0,19.0,20.0")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from", dest="source", help="source branch for --apply")
    ap.add_argument("--push", action="store_true", help="push after --apply")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.check and not args.apply:
        die("specify --check or --apply", 2)

    wanted = [e.strip() for e in args.editions.split(",") if e.strip()]
    branches = edition_branches(args.repo, wanted)
    if not branches:
        die("no edition branches found (wanted: %s)" % ", ".join(wanted))
    git(args.repo, "fetch", "-q", "origin", check=False)

    if args.check:
        result = check(args.repo, branches)
        drift = bool(result["missing"] or result["diverged"])
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("p-files per branch: %s" % ", ".join(
                "%s=%d" % (b, n) for b, n in result["counts"].items()))
            for b, files in result["missing"].items():
                print("\nMissing on %s (%d):" % (b, len(files)))
                for f in files:
                    print("  - %s" % f)
            if result["diverged"]:
                print("\nSame file, different content (%d):" % len(result["diverged"]))
                for d in result["diverged"]:
                    print("  - %s  [%s]" % (d["path"], ", ".join(d["branches"])))
            if not drift:
                print("\nNo drift: same p-files, same content.")
        return 1 if drift else 0

    if not args.source:
        die("--apply requires --from <branch> -- which branch is the source is "
            "a judgement call, not something the script can decide", 2)
    changed = apply(args.repo, branches, args.source, args.push)
    if args.json:
        print(json.dumps({"source": args.source, "changed": changed}, ensure_ascii=False))
    else:
        for b, what in changed.items():
            print("%s: %s" % (b, what))
        if not changed:
            print("Nothing to do -- the branches are already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
