# Repository privacy rules

This is a public repository for software that handles clipboard content. Treat
runtime data, logs, device configuration, credentials and local research as private.

- Use synthetic clipboard fixtures and isolated temporary databases, peers and
  keychains. Never copy real clipboard history, screenshots, logs or credentials
  into source, tests, commit messages, PRs or build artifacts.
- Read `docs/publication.md` before committing or publishing. Install the reviewed
  hooks after cloning. Use `uv` for Python execution and dependencies.
- Review the exact staged files. Stage explicit paths; preserve unrelated and
  untracked work. Never force-add ignored agent state, research or runtime files.
- Use reviewed public author/committer identities. Never bypass a failing privacy
  hook, disable its scanner, add broad exceptions, or reinstall changed controls
  merely to make a failure disappear. Diagnose the failure and review the change.
- Keep local private literals in the ignored `.privacy-denylist`. Do not publish
  those literals in a shared rule, test fixture, diagnosis or report.
- Review opaque assets and record their exact SHA-256. A filename alone is not
  approval. Check the actual packaged artifact before uploading it.
- Keep vulnerability research in ignored local handoffs or an authorized private
  reporting channel. Public CI cannot undo disclosure from a public push.
- Never include clipboard payloads in logs. Respect sensitive pasteboard markers,
  validate inbound data, and derive file paths from validated local identifiers.
- Do not change unrelated keychain permissions, inspect real user history, or
  purge user data as part of testing. Preserve running-service state when tests
  require interruption. Record tests and any unverified device/UI behavior.

Local hooks are an accident barrier. A publisher who can bypass hooks or upload
directly still needs deliberate review; these checks cannot prove all prose,
images, audio or compiled content contains no private information.
