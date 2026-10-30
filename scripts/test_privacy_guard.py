"""Exercise real hooks and Git pushes, exclusively in temporary repositories."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
SCANNER = shutil.which("gitleaks") or str(SOURCE / ".git/tools/gitleaks")
TOKEN = "gh" + "p_" + "AbCdE12345" * 3 + "AbCdE1"
IDENTITY = "test-author <test-author@users.noreply.github.com>"


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clipbar-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(UV_CACHE_DIR="/tmp/uv-cache", GITLEAKS_BIN=SCANNER,
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        self.git("init", "-q", "--template=", "-b", "main")
        self.git("config", "user.name", "test-author")
        self.git("config", "user.email", "test-author@users.noreply.github.com")
        (self.root / "scripts").mkdir()
        for name in ("privacy-check.py", "install-hooks.sh", "leak-check.sh"):
            shutil.copyfile(SOURCE / "scripts" / name, self.root / "scripts" / name)
        for name in (".gitignore", ".gitleaks.toml"):
            shutil.copyfile(SOURCE / name, self.root / name)
        self.policy = {"identities": [IDENTITY], "reviewed_assets": {}}
        self.install()
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def command(self, args, *, ok=True, data=None, env=None):
        result = subprocess.run(args, cwd=self.root, env=env or self.env, input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(TOKEN.encode(), result.stdout + result.stderr)
            self.assertNotIn(b"Traceback", result.stdout + result.stderr)
        return result

    def git(self, *args, **kwargs):
        return self.command(["git", *args], **kwargs)

    def install(self):
        (self.root / ".privacy-policy.json").write_text(json.dumps(self.policy))
        self.command(["bash", "scripts/install-hooks.sh"])

    def stage(self, text, name="sample.txt"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        self.git("add", "-f", "--", name)

    def check(self, *args, **kwargs):
        return self.command(["bash", "scripts/leak-check.sh", *args], **kwargs)

    def unchecked_commit(self, message="Synthetic unsafe commit", env=None):
        self.git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", message, env=env)

    def push_check(self, ref="HEAD", old=None, **kwargs):
        oid = self.git("rev-parse", ref).stdout.strip()
        line = b"refs/heads/test " + oid + b" refs/heads/test " + (old or b"0" * 40) + b"\n"
        return self.check("push", data=line, **kwargs)

    def test_clean_commit_and_actual_local_push(self):
        self.stage("ordinary source")
        self.git("commit", "-qm", "Clean synthetic change")
        remote = Path(self.temp.name) / "remote.git"
        self.git("init", "--bare", "-q", str(remote))
        self.git("push", str(remote), "main")
        self.assertTrue((remote / "refs/heads/main").exists())

    def test_index_not_worktree_and_partial_staging(self):
        self.stage(TOKEN)
        (self.root / "sample.txt").write_text("safe unstaged replacement")
        self.check(ok=False)
        self.git("add", "sample.txt")
        (self.root / "sample.txt").write_text(TOKEN)
        self.check()  # unstaged content is not included in a commit

    def test_force_added_ignored_path(self):
        self.stage("synthetic", ".agents/private.txt")
        self.check(ok=False)

    def test_changed_ignore_and_scanner_rules_cannot_weaken_installed_guard(self):
        (self.root / ".gitignore").write_text("")
        (self.root / ".gitleaks.toml").write_text('title = "weakened"\n')
        (self.root / "scripts/privacy-check.py").write_text("raise SystemExit(0)\n")
        self.stage(TOKEN)
        self.git("add", "-A")
        self.check(ok=False)
        self.git("rm", "-f", "sample.txt")
        self.stage("private", ".agents/private.txt")
        self.check(ok=False)

    def test_author_and_committer_overrides(self):
        self.stage("safe")
        for role in ("AUTHOR", "COMMITTER"):
            with self.subTest(role=role):
                env = dict(self.env, **{f"GIT_{role}_NAME": "Unreviewed Name"})
                self.git("commit", "-qm", "Safe message", env=env, ok=False)

    def test_commit_message_secret(self):
        self.git("commit", "--allow-empty", "-qm", TOKEN, ok=False)

    def test_add_then_delete_history_blocked_before_actual_push(self):
        self.stage(TOKEN)
        self.unchecked_commit()
        self.git("rm", "-f", "sample.txt")
        self.unchecked_commit()
        self.check()  # clean tree does not imply clean outgoing history
        remote = Path(self.temp.name) / "remote.git"
        self.git("init", "--bare", "-q", str(remote))
        self.git("push", str(remote), "main", ok=False)
        self.assertFalse((remote / "refs/heads/main").exists())

    def test_outgoing_message_only_and_identity(self):
        self.unchecked_commit(TOKEN)
        self.push_check(old=self.base, ok=False)
        self.git("reset", "--hard", self.base.decode())
        self.unchecked_commit(env=dict(self.env, GIT_AUTHOR_NAME="Unreviewed Name"))
        self.push_check(old=self.base, ok=False)

    def test_tag_message_and_tagger(self):
        self.git("tag", "-a", "synthetic", "-m", TOKEN)
        self.push_check("synthetic", ok=False)
        self.git("tag", "-d", "synthetic")
        self.git("tag", "-a", "synthetic", "-m", "Safe tag",
                 env=dict(self.env, GIT_COMMITTER_NAME="Unreviewed Name"))
        self.push_check("synthetic", ok=False)

    def test_new_branch_unknown_remote_and_deletion(self):
        self.push_check()
        self.push_check(old=b"f" * 40)
        self.check("push", data=b"(delete) " + b"0" * 40 + b" refs/heads/test " + self.base + b"\n")

    def test_rename_and_delete(self):
        self.stage("clean")
        self.git("commit", "-qm", "Add clean file")
        self.git("mv", "sample.txt", "renamed.txt")
        self.check()
        self.git("rm", "-f", "renamed.txt")
        self.check()

    def test_opaque_index_and_hash_bound_review(self):
        data = b"\0synthetic image\xff"
        self.stage(data, "fixture.png")
        (self.root / "fixture.png").write_text("safe unstaged replacement")
        self.check(ok=False)
        self.policy["reviewed_assets"]["fixture.png"] = hashlib.sha256(data).hexdigest()
        self.install()
        self.check()
        self.stage(data + b"changed", "fixture.png")
        self.check(ok=False)

    def test_reviewed_binary_does_not_suppress_credentials(self):
        data = b"\0" + TOKEN.encode()
        self.policy["reviewed_assets"]["fixture.png"] = hashlib.sha256(data).hexdigest()
        self.install()
        self.stage(data, "fixture.png")
        self.check(ok=False)

    def test_private_denylist_and_local_exclusion(self):
        (self.root / ".privacy-denylist").write_text("synthetic-private-phrase\n")
        self.stage("x" * 200000 + "synthetic-private-phrase")
        self.check(ok=False)
        self.git("rm", "-f", "sample.txt")
        (self.root / ".git/info").mkdir(exist_ok=True)
        (self.root / ".git/info/exclude").write_text("private-transcript.txt\n")
        self.stage("innocent-looking prose", "private-transcript.txt")
        self.check(ok=False)

    def test_scanner_fallback_and_missing(self):
        env = dict(self.env, GITLEAKS_BIN="/nonexistent/scanner")
        self.check(env=env, ok=False)
        (self.root / ".git/tools").mkdir()
        (self.root / ".git/tools/gitleaks").symlink_to(SCANNER)
        self.check(env=env)
        self.stage(TOKEN)
        self.check(env=env, ok=False)

    def test_symlink_rejected(self):
        (self.root / "linked").symlink_to("sample.txt")
        self.git("add", "linked")
        self.check(ok=False)

    def artifact(self, members):
        artifact = Path(self.temp.name) / "release.zip"
        with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        return str(artifact)

    def test_actual_archive_contents_and_digest(self):
        artifact = self.artifact({"README.md": "synthetic release"})
        result = self.check("artifact", artifact)
        self.assertIn(hashlib.sha256(Path(artifact).read_bytes()).hexdigest().encode(), result.stdout)
        artifact = self.artifact({"README.md": TOKEN})
        self.check("artifact", artifact, ok=False)

    def test_archive_ignored_paths_and_traversal(self):
        for name in (".env", "data/history.db", "../outside.txt", "/absolute.txt"):
            with self.subTest(name=name):
                self.check("artifact", self.artifact({name: "synthetic"}), ok=False)

    def test_artifact_opaque_approval_and_replacement(self):
        data = b"\0synthetic executable\xff"
        artifact = self.artifact({"ClipBar.app/Contents/MacOS/ClipBar": data})
        self.check("artifact", artifact, ok=False)
        approvals = Path(self.temp.name) / "review.json"
        approvals.write_text(json.dumps({"ClipBar.app/Contents/MacOS/ClipBar": hashlib.sha256(data).hexdigest()}))
        self.check("artifact", artifact, str(approvals))
        artifact = self.artifact({"ClipBar.app/Contents/MacOS/ClipBar": data + b"changed"})
        self.check("artifact", artifact, str(approvals), ok=False)

    def test_archive_symlink_and_tar_secret(self):
        artifact = Path(self.temp.name) / "release.zip"
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(info, "/synthetic/outside")
        self.check("artifact", str(artifact), ok=False)
        artifact = Path(self.temp.name) / "release.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            info = tarfile.TarInfo("readme.txt")
            info.size = len(TOKEN)
            archive.addfile(info, io.BytesIO(TOKEN.encode()))
        self.check("artifact", str(artifact), ok=False)


if __name__ == "__main__":
    unittest.main()
