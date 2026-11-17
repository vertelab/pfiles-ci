#!/usr/bin/env python3
"""Tests for the pfiles scripts.

Run with:
    python3 -m pytest tests/ -q
    python3 tests/test_pfiles.py          # no pytest needed

The tests use throwaway git repositories and never touch a real one. The
install script is tested for its classification logic only -- it needs a
working Odoo, which is the CI job's business, not a unit test's.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")


def load(name):
    """Import a script by path (they are not a package)."""
    path = os.path.join(SCRIPTS, name)
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_mod = load("pfiles-check.py")
editions_mod = load("pfiles-editions.py")
expand_mod = load("pfiles-expand.py")
sync_mod = load("pfiles-sync.py")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True, check=check)


class Repo:
    """A throwaway git repo with edition branches."""

    def __init__(self, branches=("18.0",), files=None):
        self.path = tempfile.mkdtemp(prefix="pfiles-test-")
        git(self.path, "init", "-q", "-b", branches[0], ".")
        git(self.path, "config", "user.email", "t@t")
        git(self.path, "config", "user.name", "t")
        git(self.path, "config", "commit.gpgsign", "false")
        self.write(".gitkeep", "")
        self.commit("init")
        for b in branches[1:]:
            git(self.path, "branch", b)
        for name, content in (files or {}).items():
            self.write(name, content)

    def write(self, rel, content):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full) or self.path, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)

    def commit(self, msg):
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", msg)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestPfileSplit(unittest.TestCase):
    """The '.p' sits BEFORE the extension -- the classic trap."""

    def test_p_py(self):
        self.assertEqual(check_mod.pfile_split("a.p.py"), ("a.py", ".p.py"))

    def test_p_xml(self):
        self.assertEqual(check_mod.pfile_split("a.p.xml"), ("a.xml", ".p.xml"))

    def test_double_extension_is_wrong(self):
        # a.py.p.py must not be produced by pepfile_hint; if it appears, the
        # split yields a.py.py and creates a new defect.
        self.assertEqual(check_mod.pfile_split("a.py.p.py"),
                         ("a.py.py", ".p.py"))

    def test_non_pfile(self):
        self.assertEqual(check_mod.pfile_split("a.py"), (None, None))


class TestPepfileHint(unittest.TestCase):
    """The hint must insert '.p' before the extension, not append it."""

    def test_py(self):
        self.assertEqual(check_mod.pepfile_hint("test_x.py"), "test_x.p.py")

    def test_xml(self):
        self.assertEqual(check_mod.pepfile_hint("a.xml"), "a.p.xml")

    def test_nested(self):
        self.assertEqual(check_mod.pepfile_hint("models/crm.py"),
                         "models/crm.p.py")

    def test_roundtrip(self):
        # The hint must survive pfile_split unchanged.
        for name in ("a.py", "a.xml", "a.csv", "b/c.js"):
            hinted = check_mod.pepfile_hint(name)
            self.assertEqual(check_mod.pfile_split(hinted)[0], name,
                             "%s -> %s does not round-trip" % (name, hinted))


class TestEditionProfile(unittest.TestCase):
    """18/19 are legacy, 20 is modern."""

    def test_legacy(self):
        for v in ("18.0", "19.0"):
            p = check_mod.edition_profile(v)
            self.assertEqual(p["security_format"], "legacy")
            self.assertEqual(p["security_filename"], "ir.model.access.csv")

    def test_modern(self):
        p = check_mod.edition_profile("20.0")
        self.assertEqual(p["security_format"], "modern")
        self.assertEqual(p["security_filename"], "ir.access.csv")

    def test_unknown_edition_skips_checks(self):
        self.assertIsNone(check_mod.edition_profile("99.0"))


class TestSecurityFindings(unittest.TestCase):
    """The security checks, against real files on disk."""

    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.cleanup()

    def _findings(self, version, filename, header, manifest_data=None):
        self.repo.write("mod/__init__.py", "")
        self.repo.write("mod/security/" + filename, header + "\n")
        data = manifest_data if manifest_data is not None else ["security/" + filename]
        self.repo.write("mod/__manifest__.py",
                        "{'name': 'M', 'data': %r}" % (data,))
        findings, _info = check_mod.security_findings(
            os.path.join(self.repo.path, "mod"), version)
        return findings

    LEGACY = "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink"
    MODERN = "id,name,model_id,group_id/id,operation,domain"

    def test_legacy_file_on_20_fails(self):
        f = self._findings("20.0", "ir.model.access.csv", self.LEGACY)
        kinds = [x[1] for x in f]
        self.assertIn("WRONG_SECURITY_FORMAT", kinds)

    def test_modern_file_on_18_fails(self):
        f = self._findings("18.0", "ir.access.csv", self.MODERN)
        self.assertIn("WRONG_SECURITY_FORMAT", [x[1] for x in f])

    def test_correct_file_passes(self):
        self.assertEqual(self._findings("20.0", "ir.access.csv", self.MODERN), [])
        self.assertEqual(self._findings("18.0", "ir.model.access.csv", self.LEGACY), [])

    def test_unlisted_other_edition_file_is_harmless(self):
        # The side-by-side layout: the file for the OTHER edition sits in
        # security/ but is not loaded. That must not be an error.
        self.repo.write("mod/__init__.py", "")
        self.repo.write("mod/security/ir.access.csv", self.MODERN + "\n")
        self.repo.write("mod/security/ir.model.access.csv", self.LEGACY + "\n")
        self.repo.write("mod/__manifest__.py",
                        "{'name': 'M', 'data': ['security/ir.model.access.csv']}")
        findings, _ = check_mod.security_findings(
            os.path.join(self.repo.path, "mod"), "18.0")
        self.assertEqual(findings, [],
                         "an unlisted file for the other edition must be harmless")

    def test_uid_domain_on_20_fails(self):
        row = "a1,r,res.partner,base.group_user,crud,[('user_id','=',uid)]"
        f = self._findings("20.0", "ir.access.csv", self.MODERN + "\n" + row)
        self.assertIn("BAD_DOMAIN_NAME", [x[1] for x in f])

    def test_user_id_domain_on_20_passes(self):
        row = "a1,r,res.partner,base.group_user,crud,[('user_id','=',user.id)]"
        f = self._findings("20.0", "ir.access.csv", self.MODERN + "\n" + row)
        self.assertEqual(f, [])

    def test_domain_with_commas_is_joined(self):
        # Odoo writes the domain unquoted, so csv splits it. A naive row[idx]
        # read would miss uid in the tail.
        row = "a1,r,res.partner,base.group_user,crud,[('user_id','=',uid),('a','=',1)]"
        f = self._findings("20.0", "ir.access.csv", self.MODERN + "\n" + row)
        self.assertIn("BAD_DOMAIN_NAME", [x[1] for x in f],
                      "uid stranded after a comma must still be found")


class TestEditionsConfig(unittest.TestCase):
    """Reading .pfiles-ci."""

    def setUp(self):
        self.repo = Repo(branches=("18.0", "20.0"))

    def tearDown(self):
        self.repo.cleanup()

    def test_reads_editions(self):
        self.repo.write(".pfiles-ci", "pfiles-ci: 1\neditions: 18.0 20.0\n")
        self.assertEqual(editions_mod.parse_config(
            os.path.join(self.repo.path, ".pfiles-ci")), ["18.0", "20.0"])

    def test_comma_separated(self):
        self.repo.write(".pfiles-ci", "editions: 18.0,20.0\n")
        self.assertEqual(editions_mod.parse_config(
            os.path.join(self.repo.path, ".pfiles-ci")), ["18.0", "20.0"])

    def test_deduplicates(self):
        self.repo.write(".pfiles-ci", "editions: 18.0 18.0 20.0\n")
        self.assertEqual(editions_mod.parse_config(
            os.path.join(self.repo.path, ".pfiles-ci")), ["18.0", "20.0"])

    def test_reads_deps(self):
        self.repo.write(".pfiles-ci",
                        "editions: 18.0\ndeps: OCA/dms odoo-base\n")
        editions_mod.parse_config(os.path.join(self.repo.path, ".pfiles-ci"))
        self.assertEqual(editions_mod.DEPENDENCIES, ["OCA/dms", "odoo-base"])

    def test_comments_ignored(self):
        self.repo.write(".pfiles-ci", "# a comment\neditions: 18.0\n")
        self.assertEqual(editions_mod.parse_config(
            os.path.join(self.repo.path, ".pfiles-ci")), ["18.0"])

    def test_missing_file_returns_none(self):
        self.assertIsNone(editions_mod.parse_config(
            os.path.join(self.repo.path, ".pfiles-ci")))

    def test_branches_found(self):
        have = editions_mod.branches(self.repo.path)
        self.assertIn("18.0", have)
        self.assertIn("20.0", have)

    def test_missing_branch_absent(self):
        self.assertNotIn("19.0", editions_mod.branches(self.repo.path))


class TestSyncCheck(unittest.TestCase):
    """Drift between edition branches."""

    def setUp(self):
        self.repo = Repo(branches=("18.0", "20.0"))

    def tearDown(self):
        self.repo.cleanup()

    def test_no_drift_when_identical(self):
        self.repo.write("mod/a.p.py", "X = 1\n")
        self.repo.commit("a")
        git(self.repo.path, "checkout", "-q", "20.0")
        self.repo.write("mod/a.p.py", "X = 1\n")
        self.repo.commit("a")
        git(self.repo.path, "checkout", "-q", "18.0")
        result = sync_mod.check(self.repo.path, ["18.0", "20.0"])
        self.assertEqual(result["missing"], {})
        self.assertEqual(result["diverged"], [])

    def test_missing_file_detected(self):
        self.repo.write("mod/a.p.py", "X = 1\n")
        self.repo.commit("a")
        result = sync_mod.check(self.repo.path, ["18.0", "20.0"])
        self.assertIn("20.0", result["missing"])
        self.assertIn("mod/a.p.py", result["missing"]["20.0"])

    def test_diverged_content_detected(self):
        self.repo.write("mod/a.p.py", "X = 1\n")
        self.repo.commit("a")
        git(self.repo.path, "checkout", "-q", "20.0")
        self.repo.write("mod/a.p.py", "X = 2\n")
        self.repo.commit("a")
        git(self.repo.path, "checkout", "-q", "18.0")
        result = sync_mod.check(self.repo.path, ["18.0", "20.0"])
        paths = [d["path"] for d in result["diverged"]]
        self.assertIn("mod/a.p.py", paths)


class TestExpand(unittest.TestCase):
    """Expansion output naming and the module copy."""

    def test_output_name(self):
        self.assertEqual(expand_mod.output_name("a/b.p.py"), "a/b.py")
        self.assertEqual(expand_mod.output_name("a/b.p.xml"), "a/b.xml")

    def test_output_name_leaves_plain_file_alone(self):
        self.assertEqual(expand_mod.output_name("a/b.py"), "a/b.py")


class TestInstallClassification(unittest.TestCase):
    """The install script must tell ENV from MODULE from OK."""

    def test_script_is_syntactically_valid(self):
        proc = subprocess.run(["bash", "-n", os.path.join(SCRIPTS, "pfiles-install.sh")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_usage_requires_arguments(self):
        proc = subprocess.run(["bash", os.path.join(SCRIPTS, "pfiles-install.sh")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_rejects_malformed_edition(self):
        proc = subprocess.run(
            ["bash", os.path.join(SCRIPTS, "pfiles-install.sh"), "twenty", "/tmp"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("20.0", proc.stderr)

    def test_missing_preprocess_is_environment_error(self):
        # With PFILES_DB_FRESH=1 and no psql, the script must report ENV (4),
        # never OK -- a false green here approves a module nobody tested.
        # A psql-less environment must give ENV (4), never OK -- a false green
        # here approves a module nobody tested. PFILES_OCB_SRC points at a
        # directory without odoo-bin so the script fails fast instead of
        # cloning OCB.
        tmp = tempfile.mkdtemp(prefix="pfiles-noocb-")
        try:
            env = dict(os.environ, PFILES_DB_FRESH="1",
                       PATH="/usr/bin:/bin", PFILES_OCB_SRC=tmp)
            proc = subprocess.run(
                ["bash", os.path.join(SCRIPTS, "pfiles-install.sh"), "18.0", tmp],
                capture_output=True, text=True, env=env, timeout=120)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn(proc.returncode, (4, 5),
                      "must not report OK without a testable environment: %s"
                      % proc.stderr[-300:])


class TestNoSwedishInScripts(unittest.TestCase):
    """Source code and commits are English; UI strings are translated."""

    def test_scripts_are_english(self):
        offenders = []
        for name in os.listdir(SCRIPTS):
            path = os.path.join(SCRIPTS, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if any(c in line for c in "åäöÅÄÖ"):
                        offenders.append("%s:%d" % (name, lineno))
        self.assertEqual(offenders, [],
                         "Swedish characters in source: %s" % ", ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
