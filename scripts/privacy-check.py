#!/usr/bin/env python3
"""Accidental-publication barrier, installed with reviewed policy in .git/privacy.

Never print scanned content, filenames, metadata or scanner diagnostics.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

POLICY_DIR = Path(__file__).resolve().parent
MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 20000


class Blocked(Exception):
    pass


def run(args, *, data=None, cwd=None, env=None, ok=(0,)):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd, env=env)
    if result.returncode not in ok:
        raise Blocked("required command failed (diagnostics withheld for privacy)")
    return result


def git(*args):
    return run(["git", "--no-replace-objects", *args]).stdout


def object_bytes(oid):
    if int(git("cat-file", "-s", oid)) > MAX_BYTES:
        raise Blocked("object exceeds the review limit")
    return git("cat-file", "-p", oid)


class Check:
    def __init__(self, temp):
        self.root = Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip())
        self.policy = json.loads((POLICY_DIR / ".privacy-policy.json").read_text())
        self.parts, self.seen, self.denied = [], set(), []
        self.total = 0
        for path in (POLICY_DIR / ".privacy-denylist", self.root / ".privacy-denylist"):
            if path.exists():
                self.denied.extend(line.lower() for line in path.read_bytes().splitlines()
                                   if line.strip() and not line.startswith(b"#"))
        # Git's own ignore matcher, using installed policy plus local exclusions.
        self.ignore_repo = Path(temp) / "ignore"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        run(["git", "init", "-q", "--template=", str(self.ignore_repo)], env=self.env)
        shutil.copyfile(POLICY_DIR / ".gitignore", self.ignore_repo / ".gitignore")
        exclude = Path(os.fsdecode(git("rev-parse", "--git-path", "info/exclude")).strip())
        if exclude.exists():
            (self.ignore_repo / ".git/info").mkdir(exist_ok=True)
            shutil.copyfile(exclude, self.ignore_repo / ".git/info/exclude")

    def add(self, data):
        self.total += len(data)
        if self.total > MAX_BYTES:
            raise Blocked("scan exceeds the review limit; review in smaller batches")
        if any(value in data.lower() for value in self.denied):
            raise Blocked("private denylist match")
        # Gitleaks skips binary-looking stdin. Normalize controls/invalid UTF-8
        # before scanning; exact-byte denylist checks above still see raw bytes.
        text = data.decode("utf-8", errors="replace")
        self.parts.append("".join(c if c.isprintable() or c in "\n\r\t" else "\n"
                                  for c in text).encode("utf-8"))

    def identity(self, data):
        match = re.fullmatch(rb"(.+ <[^<>\n]+>) \d+ [+-]\d{4}", data.strip())
        if not match or match[1].decode("utf-8") not in self.policy["identities"]:
            raise Blocked("unreviewed author, committer, or tagger identity")

    def paths(self, names):
        if not names:
            return
        result = run(["git", "-c", "core.excludesFile=/dev/null", "check-ignore",
                      "--no-index", "-z", "--stdin"], data=b"\0".join(names) + b"\0",
                     cwd=self.ignore_repo, env=self.env, ok=(0, 1))
        if result.returncode == 0:
            raise Blocked("local, generated, or private path would be published")
        for name in names:
            self.add(name)

    def blob(self, name, data, mode=b"100644", approvals=None):
        if mode not in (b"100644", b"100755"):
            raise Blocked("symlinks and submodules require a separate publication review")
        self.add(data)
        try:
            text = data.decode("utf-8")
            opaque = any(ord(c) < 32 and c not in "\t\n\r" for c in text)
        except UnicodeDecodeError:
            opaque = True
        opaque |= PurePosixPath(os.fsdecode(name)).suffix.lower() in {
            ".svg", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".wav", ".mp3",
            ".mp4", ".zip", ".gz", ".tgz"}
        if opaque:
            approved = approvals if approvals is not None else self.policy["reviewed_assets"]
            if approved.get(os.fsdecode(name)) != hashlib.sha256(data).hexdigest():
                raise Blocked("opaque asset has no review for these exact bytes")
            # Approval cannot suppress readable credential scanning in binaries.
            self.add(b"\n".join(re.findall(rb"[\x20-\x7e]{4,}", data)))
            for encoding in ("utf-16-le", "utf-16-be"):
                self.add(data.decode(encoding, errors="ignore").encode("utf-8"))

    def tree(self, tree):
        names = []
        for entry in filter(None, git("ls-tree", "-r", "-z", tree).split(b"\0")):
            meta, name = entry.split(b"\t", 1)
            mode, kind, oid = meta.split()
            names.append(name)
            if kind != b"blob":
                raise Blocked("submodule requires a separate publication review")
            key = (mode, oid, name)
            if key not in self.seen:
                self.blob(name, object_bytes(oid.decode()), mode)
                self.seen.add(key)
        self.paths(names)

    def staged(self):
        for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            value = git("var", role).strip()
            self.identity(value)
            self.add(value)
        # Complete index: partial staging, renames and deletions use exact blobs.
        self.tree(git("write-tree").decode().strip())

    def push(self, lines):
        if git("rev-parse", "--is-shallow-repository").strip() != b"false":
            raise Blocked("fetch complete history before publishing")
        grafts = Path(os.fsdecode(git("rev-parse", "--git-path", "info/grafts")).strip())
        if grafts.exists():
            raise Blocked("remove local history grafts before publication review")
        for line in lines:
            fields = line.split()
            if len(fields) != 4:
                raise Blocked("malformed pre-push input")
            local_ref, local_oid, remote_ref, remote_oid = fields
            self.add(local_ref + b"\n" + remote_ref)
            if set(local_oid) == {48}:
                continue
            oid = local_oid.decode()
            kind = git("cat-file", "-t", oid).strip()
            while kind == b"tag":
                raw = object_bytes(oid)
                self.add(raw)
                header = raw.split(b"\n\n", 1)[0].splitlines()
                for row in header:
                    if row.startswith(b"tagger "):
                        self.identity(row[7:])
                oid = header[0].split()[1].decode()
                kind = git("cat-file", "-t", oid).strip()
            if kind != b"commit":
                raise Blocked("only refs pointing to commits may be published")
            revisions = [oid]
            if set(remote_oid) != {48}:
                old = run(["git", "--no-replace-objects", "rev-parse", "--verify",
                           remote_oid.decode() + "^{commit}"], ok=(0, 128))
                if old.returncode == 0:
                    revisions.append("^" + old.stdout.decode().strip())
                # Unknown remote tip: conservatively inspect the full history.
            for commit in git("rev-list", *revisions).decode().splitlines():
                raw = object_bytes(commit)
                self.add(raw)  # messages and signatures, not only patch text
                for row in raw.split(b"\n\n", 1)[0].splitlines():
                    row = row.lstrip()  # signed merge-tag headers may be indented
                    if row.startswith((b"author ", b"committer ", b"tagger ")):
                        self.identity(row.split(b" ", 1)[1])
                self.tree(commit)

    def artifact(self, filename, approvals):
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise Blocked("artifact must be a bounded regular file")
        data = path.read_bytes()  # scan and hash the same bytes
        self.add(os.fsencode(path.name))
        self.add(data)
        count = 0

        def member(name, content):
            nonlocal count
            count += 1
            parts = PurePosixPath(name)
            if count > MAX_ENTRIES or parts.is_absolute() or ".." in parts.parts or "\\" in name:
                raise Blocked("unsafe archive member or excessive archive size")
            self.paths([os.fsencode(name)])
            self.blob(os.fsencode(name), content, approvals=approvals)

        # Never extract submitted paths. Nested archives require their own review.
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                size = 0
                if len(archive.infolist()) > MAX_ENTRIES:
                    raise Blocked("archive exceeds the review limit")
                for item in archive.infolist():
                    size += item.file_size
                    if size > MAX_BYTES:
                        raise Blocked("archive exceeds the review limit")
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise Blocked("archive symlink is not allowed")
                    if not item.is_dir():
                        member(item.filename, archive.read(item))
        elif tarfile.is_tarfile(io.BytesIO(data)):
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                size = 0
                for item in archive:
                    size += item.size
                    count += 1
                    if size > MAX_BYTES or count > MAX_ENTRIES:
                        raise Blocked("archive exceeds the review limit")
                    if item.isdir():
                        continue
                    if not item.isfile():
                        raise Blocked("archive links and special files are not allowed")
                    member(item.name, archive.extractfile(item).read())
        else:
            member(path.name, data)
        return hashlib.sha256(data).hexdigest()

    def scan(self):
        scanner = shutil.which(os.environ.get("GITLEAKS_BIN", "gitleaks"))
        if not scanner:
            candidate = Path(os.fsdecode(git("rev-parse", "--git-path", "tools/gitleaks")).strip())
            if candidate.is_file() and os.access(candidate, os.X_OK):
                scanner = str(candidate.resolve())
        if not scanner:
            raise Blocked("install Gitleaks 8.30.1 or newer")
        version = run([scanner, "version"]).stdout.decode().strip().lstrip("v")
        match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (8, 30, 1):
            raise Blocked("Gitleaks 8.30.1 or newer is required")
        result = run([scanner, "stdin", "--config", str(POLICY_DIR / ".gitleaks.toml"),
                      "--redact=100", "--no-banner", "--ignore-gitleaks-allow",
                      "--gitleaks-ignore-path", "/dev/null", "--max-decode-depth", "5"],
                     data=b"\n".join(self.parts), ok=(0, 1))
        if result.returncode:
            raise Blocked("credential or personal-information pattern detected")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="clipbar-privacy-") as temp:
            check = Check(temp)
            mode, *args = sys.argv[1:]
            digest = None
            if mode == "staged":
                check.staged()
            elif mode == "message":
                check.add(Path(args[0]).read_bytes())
            elif mode == "push":
                check.push(sys.stdin.buffer)
            elif mode == "artifact":
                approved = json.loads(Path(args[1]).read_text()) if len(args) > 1 else {}
                digest = check.artifact(args[0], approved)
            else:
                raise Blocked("unknown check mode")
            check.scan()
            print("Privacy check passed" + ("; artifact SHA-256: " + digest if digest else ""))
    except Exception as error:
        # Includes encrypted/corrupt archives and unexpected dependency failures.
        # A traceback can itself disclose local paths or submitted filenames.
        print("Privacy check blocked: " + (str(error) if isinstance(error, Blocked)
                                          else "invalid input or unavailable dependency"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
