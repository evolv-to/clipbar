# Publishing safely

Use a public alias and GitHub noreply email, and review the permitted identities
in `.privacy-policy.json`. Install `uv` and Gitleaks 8.30.1 or newer, then run
`./scripts/install-hooks.sh` after reviewing the scripts and policy. The installer
copies them into `.git/privacy` and installs pre-commit, commit-msg and pre-push
hooks. Git hooks are local and are not automatically installed by cloning.

The installed snapshot checks the complete index, effective author/committer,
commit messages, outgoing commits (including files later deleted), tag metadata,
and ref names. New branches are checked through their full available history.
Shallow histories and history grafts must be resolved before pushing. Ignored
paths, symlinks and submodules are blocked. Missing dependencies or incomplete
checks block the operation. Scanner output is suppressed to avoid printing the
very content being protected.

Changes to a working copy's guard or policy do not affect the installed snapshot.
After reviewing and testing an intentional policy change, run the installer again
to adopt it. This explicit refresh is a trust decision. Do not reinstall a proposed
weakening just to unblock its own commit. Custom `core.hooksPath` installations
need deliberate integration; the installer refuses to silently override them.

## Local private material

Keep private research under ignored `.agents/`, and add exact local exclusions to
`.git/info/exclude` for research kept elsewhere. `.privacy-denylist` contains one
private literal per line, matched case-insensitively; blank lines and lines starting
with `#` are ignored. Its contents must stay local. Both the installed copy and
current local file are checked. Do not place private values in the public policy.

The shared rules detect common credentials, personal home paths, personal email
providers, and tailnet addresses. They do not recognize every corporate address,
person's name, private prose or encoded payload. Review content as well as scans.
The approved source WAV is pinned to its initial audited release hash. Replacing
it or adding any opaque media requires a new content review and hash approval.

## Build and release artifacts

Source checks do not cover a later build. Inspect the actual package, including
resources, debug information, embedded paths, configuration and archive metadata.
Keep build output outside source control. In an ignored local JSON file, record
reviewed opaque members as `{"archive/member/path": "sha256-of-that-member"}`.
Each nested archive must be reviewed separately before approving its hash. Do
not generate approvals from an unreviewed package and call that a review.

Run `./scripts/leak-check.sh artifact /path/to/release.zip /path/to/review.json`.
ZIP and TAR contents are inspected without extraction; traversal, links, ignored
runtime paths, unreviewed opaque members and detected secrets fail the check.
Plain files can also be checked. Omit the review file for all-text artifacts.
Checks are bounded to 128 MiB of scan input and 20,000 archive entries, failing
closed above the limits. Opaque approval never suppresses readable secret scans.
The output SHA-256 identifies the exact archive checked: upload that file and
verify the uploaded digest. A rebuilt or repackaged artifact needs another check.
Direct GitHub release uploads do not invoke Git hooks.

## Tests and limits

Run `uv run --no-project python -m unittest discover -s scripts -p 'test_privacy_guard.py' -v`.
Tests create temporary repositories and local bare remotes, with synthetic secrets;
they do not push to GitHub or inspect the real clipboard.

These controls reduce accidental disclosure before upload. Hooks can be bypassed
by the account that owns the checkout, and scanner rules cannot establish that
all content is nonprivate. GitHub push protection covers supported patterns;
public CI is detection after upload. For confidentiality against a bypass-capable
publisher, use private staging and a separate restricted publishing identity.
After a privacy history rewrite, replace old clones rather than merging their
history back into the clean repository.
