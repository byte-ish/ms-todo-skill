# Security

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private reporting:
[Report a vulnerability](https://github.com/byte-ish/ms-todo-skill/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker gains. Expect
an acknowledgement within a few days, and a fix or a clear explanation of why
it isn't one before any public disclosure.

## How credentials are handled

- Tokens are cached at `~/.config/ms-todo/token.json`, mode **0600**, inside a
  directory created **0700**.
- Writes are atomic: content goes to a temp file that is created with the private
  mode *before* the rename, so the token is never briefly world-readable.
- Tokens are never passed as command-line arguments — process arguments are
  visible to every user on the machine via `ps`.
- Tokens are never logged, not even at `-vv`. Debug logging prints URLs, methods
  and status codes, never `Authorization` headers or response tokens.
- The client requests the least-privileged scope that works: `Tasks.ReadWrite`.
  It does not ask for `Tasks.ReadWrite.All`, `User.Read` or `Mail.*`.
- Refresh tokens rotate. Whatever the identity platform returns most recently is
  what gets stored; the previous value is discarded.
- Access tokens are never parsed. Only the `id_token`, which is issued to this
  client, is decoded — and only to display who is signed in, never for an
  authorization decision.
- The pre-authenticated attachment upload URL is called **without** a bearer
  token, so the credential is not sent to a URL it isn't needed for.

## What this tool can do with your account

`Tasks.ReadWrite` grants full read and write access to the signed-in user's To Do
tasks and lists. It does not grant access to mail, files, calendar, contacts, or
anyone else's data.

## Revoking access

`auth logout` deletes the local token cache. That stops *this machine* from using
the credential, but does not revoke it centrally. To revoke properly — which
invalidates refresh tokens as well:

- **Personal account:** [account.live.com/consent/Manage](https://account.live.com/consent/Manage)
- **Work or school:** My Account → Privacy, or ask an administrator to remove
  the app's service principal.

## Threat model

In scope: credential handling on disk and in memory, accidental token disclosure
through logs or process arguments, and TLS/transport correctness.

Out of scope: anyone who already has your user account on the machine (0600 does
not defend against root or against you), the security of the app registration
you create in your own tenant, and Microsoft Graph itself.

## Supported versions

Security fixes land on the latest released minor version. There are no
long-term-support branches.
