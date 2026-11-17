# pfiles-ci

Shared CI gate for **p-files** — version-agnostic Odoo code where one source
file expands to the right code for each edition.

A repo calls this workflow instead of copying it. The scripts live here, not
in the calling repo, so a fix reaches everyone.

---

## Why

A p-file is the source for **all** editions:

```
crm_lead.p.py ──preprocess -D VERSION=18.0──▶ crm_lead.py  (Odoo 18)
              ──preprocess -D VERSION=20.0──▶ crm_lead.py  (Odoo 20)
```

That gives one bug fix reaching every edition at once — and five ways to
break silently:

| Failure | Consequence |
|---|---|
| Output not regenerated after a p-file change | Odoo runs old code |
| Hand-edited output | Overwritten at the next expansion; the edit is lost |
| Source file with no p-counterpart | The file is never expanded |
| 20.0-specific code without `# #if` | Works on one edition, fails on another |
| p-files drifting apart between branches | Fixes get lost when someone edits another branch |

The gate measures all five. Without it, the failures surface when someone
happens to look.

## Why maintain one source instead of one branch per edition

Odoo ships a new edition every year. Each one is a maintenance obligation:
APIs move, view formats change, security data changes shape, modules are
renamed or removed. The changes are individually small — a renamed field, a
`view_mode` that became `list`, a CSV that gained a column — but they arrive
every year, in every module, and they must be handled whether or not anyone
asked for a new feature.

The usual answer is one branch per edition. It works, and then it does not.
A bug fix written on 18.0 has to be carried to 19.0 and 20.0 by hand. Past a
few modules the branches drift: a fix lands on one, is forgotten on another,
and the difference stays invisible until someone upgrades. The maintenance
cost is not the porting — it is keeping the ports in agreement.

A p-file turns that inside out. There is **one** source, and the edition
differences live inside it as a handful of directives:

```
    # #if VERSION >= "20.0"
    'security/ir.access.csv',
    # #else
    'security/ir.model.access.csv',
    # #endif
```

A bug is fixed once and reaches every edition at expansion time. The yearly
port stops being "copy the code and adapt it" and becomes "find the places
where this edition differs, and write them down". That is a smaller,
reviewable change — and the result is still a single file that keeps working
for the editions you already support.

The directives are also why this needs a gate. Once edition differences live
in conditionals inside one file, a mistake no longer looks like a broken
branch: it looks like a file that expands cleanly and then fails to load on
exactly one edition. That is what this workflow checks, per edition, on every
change.

---

## What it checks

Five gates plus a sync check:

| # | Gate | Asks | Fails when |
|---|---|---|---|
| 1 | **Coverage** | Is every source file a p-file or generated? | A hand-written file has no p-source |
| 2 | **Expansion** | Does committed output match the p-files? | Output is stale (`STALE`) |
| 3 | **Syntax** | Are the directives written correctly? | `#if` instead of `# #if` |
| 4 | **Build** | Does it compile and parse? | `compileall` or XML parse fails |
| 5 | **Install** | Do the modules install in THEIR edition? | A module fails to load |
| — | **Sync** | Are the same p-files on every edition branch? | Drift between branches |

Gates 1–4 run once. Gate 5 runs **per edition** in a matrix, so a broken 20.0
cannot hide behind a green 18.0.

### Environment error vs broken module

Gate 5 distinguishes three outcomes:

| Outcome | Means | Fails? |
|---|---|---|
| **OK** | The module installed | No |
| **MODULE** | Odoo started, the module failed to load | **Yes** — code defect |
| **ENV** | Odoo/DB/dependency could not start | No, but counted and named |

A missing dependency must not send the developer looking inside their own
module. The job reports how many of the total were proved, and an edition
where **no** module could be proved fails — the gate then achieved nothing.

### Pinned version

Callers **pin** a version. That is deliberate: a change here does not reach
anyone until someone bumps, so a new version of the gate can be tried in one
repo at a time.

```yaml
uses: vertelab/pfiles-ci/.github/workflows/pfiles.yml@v1
```

---

## Install in a new repo

Best done **alongside a porting effort** — the repo is being touched anyway,
and the gate immediately shows what remains.

### 1. The caller

Create `.github/workflows/pfiles.yml` in the repo:

```yaml
name: pfiles

on:
  push:
    branches: ['*.0', 'ci/**', 'pfiles/**']
  pull_request:

jobs:
  pfiles:
    uses: vertelab/pfiles-ci/.github/workflows/pfiles.yml@v1
```

That is the whole file. No scripts are copied — they are fetched from this
repository.

### 2. Which editions the repo supports

Create `.pfiles-ci`:

```yaml
pfiles-ci: 1
editions: 18.0 20.0
# deps: OCA/dms odoo-base     # repositories the modules depend on, if any
```

Check that the branches exist:

```bash
for e in 18.0 19.0 20.0; do
  git rev-parse --verify --quiet "origin/$e" >/dev/null && echo "$e exists"
done
```

An edition listed in the file but without a branch is a **configuration
error** — the job fails and says so. That is not the same as a broken module.

`deps:` names the repositories the modules depend on. A bare name is assumed
to belong to `vertelab`; `OCA/dms` is a full slug. They are cloned at the
same edition branch.

### 3. Allow-list what is not yet a p-file

```bash
git clone --depth 1 https://github.com/vertelab/pfiles-ci /tmp/pfiles-ci
python3 /tmp/pfiles-ci/scripts/pfiles-check.py --version 18.0 --print-allow . \
  | sudo tee .pfiles-allow
```

The allow-list should only **shrink**. Do not add lines for new code — make
it a p-file instead.

### 4. Wire up the local pre-check

```bash
git config core.hooksPath .githooks
```

The hook comes from Salt (`odoo.pfiles`) and expands p-files on commit. Check
that it is wired: `git config core.hooksPath` must answer `.githooks`, not
nothing.

### 5. Verify

```bash
git add .github/workflows/pfiles.yml .pfiles-ci .pfiles-allow
git commit -m "ci(pfiles): connect to the shared gate"
git push
```

The first run shows what remains. Red is expected if the repo is not migrated
yet — it is the list of work.

---

## Maintaining this repository

Change the scripts here, not in the calling repos.

```bash
# 1. Edit scripts/ or .github/workflows/pfiles.yml
# 2. Verify against a real repo before moving the tag
# 3. Commit and tag
git tag -f v1 && git push --force origin v1
```

**`v1` is a moving tag.** Callers that pin `@v1` pick up the fix on their next
run. A repo that wants to freeze points at a fixed tag (`@v1.3.0`) or a
commit SHA.

The safety net is that `odoo-crm` runs this workflow on every change — a
broken version shows up there first.

### What must NOT live here

| File | Why it belongs to the repo |
|---|---|
| `.pfiles-ci` | `editions:` and `deps:` differ per repo |
| `.pfiles-allow` | the allow-list is the repo's own backlog |

---

## Files

```
.github/workflows/pfiles.yml   reusable workflow (workflow_call)
.github/workflows/test.yml     runs the test suite on every change
scripts/pfiles-check.py        coverage, expansion, security format
scripts/pfiles-editions.py     reads .pfiles-ci, builds the matrix
scripts/pfiles-expand.py       expands per edition into its own directory
scripts/pfiles-install.sh      installs a module, separates ENV from MODULE
scripts/pfiles-sync.py         measures and repairs drift between branches
tests/test_pfiles.py           36 tests, no pytest required
```

## Tests

```bash
python3 tests/test_pfiles.py      # or: python3 -m pytest tests/ -q
```

They cover the parts that have actually gone wrong:

| Area | What is asserted |
|---|---|
| Naming | `.p` goes **before** the extension; the hint round-trips through the split |
| Edition profiles | 18/19 legacy, 20 modern, unknown edition skips the checks |
| Security files | wrong shape fails; the unlisted file for the other edition is harmless |
| Domains | `uid` fails on 20, `user.id` passes, and a domain split by commas is rejoined |
| Config | editions and deps parsing, comments, dedupe, missing branches |
| Sync | identical branches pass; a missing file and differing content are both reported |
| Install | usage errors, and that a psql-less environment gives ENV, never OK |
| Language | no Swedish characters in the source |

The suite caught two real defects while it was being written: a `manifest`
variable lost during a translation, and a `reported` set that went missing the
same way. Both would have been silent crashes the moment the security checks
ran with a real manifest.

## See also

- `/skill:odoo-pfiles` — the p-file mechanics and the two workflows
- `docs/pfiles-ci.md` in `vertelab/plan` — the gates in detail
- `docs/pfiles-security-csv.md` — the security files for 18/19 vs 20
- `/srv/salt/README.md` — the hook and `core.hooksPath`
