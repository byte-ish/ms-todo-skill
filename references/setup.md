# Setup: registering the app and signing in

You need a Microsoft Entra **public client** application registration. It takes
about three minutes and costs nothing. A personal Microsoft account (outlook.com,
hotmail.com, live.com) works, as does a work or school account.

## Why you must register something

Microsoft To Do exposes **no application permissions** — every write endpoint in
the API is delegated-only, meaning a real user must be signed in. There is no
client-credentials or service-principal mode, no matter how the request is
shaped. The device code flow is how a command line tool gets that delegated token
without needing a browser on the machine it runs on.

---

## 1. Create the app registration

1. Go to the [Microsoft Entra admin center → App registrations](https://go.microsoft.com/fwlink/?linkid=2083908)
   and choose **New registration**.
2. **Name**: anything — `ms-todo-cli` is fine. Only you will see it.
3. **Supported account types**: pick based on the account whose tasks you want:

   | Account | Choose |
   | --- | --- |
   | Personal (outlook.com / hotmail.com / live.com) | *Personal Microsoft accounts only* |
   | Work or school | *Accounts in this organizational directory only* |
   | Both | *Accounts in any organizational directory and personal Microsoft accounts* |

4. **Redirect URI**: leave it empty. The device code flow does not use one.
5. **Register**.

Copy the **Application (client) ID** from the overview page. That is your
`--client-id`.

## 2. Turn on public client flows

This is the step people forget, and skipping it produces a confusing
`invalid_client` error at sign-in.

1. In the app, open **Authentication**.
2. Scroll to **Advanced settings → Allow public client flows**.
3. Set it to **Yes**, then **Save**.

## 3. Add the Graph permission

1. Open **API permissions → Add a permission → Microsoft Graph → Delegated permissions**.
2. Search for `Tasks` and tick **Tasks.ReadWrite**.
3. **Add permissions**.

`Tasks.ReadWrite` is the least-privileged scope that covers everything this tool
does. Add `Tasks.ReadWrite.Shared` only if you need to touch lists other people
have shared with you.

On a work or school tenant your administrator may need to click **Grant admin
consent**. On a personal account you consent yourself at sign-in.

## 4. Pick your tenant value

| Situation | `--tenant` |
| --- | --- |
| Personal Microsoft account | `consumers` |
| Work or school account | your tenant id or domain, e.g. `contoso.onmicrosoft.com` |
| Either, or you're not sure | `common` (the default) |

Using `common` works in most cases. Naming your tenant explicitly is slightly
faster and avoids the account picker.

## 5. Sign in

```bash
python3 scripts/todo.py auth login --client-id <application-client-id> --save
```

`--save` writes the client id and tenant to `~/.config/ms-todo/config.json` so
you never pass them again. The command prints something like:

```
  To sign in, open https://microsoft.com/devicelogin
  and enter code:  F7QK9TXBM

  The code expires in 15 minutes.
```

Open that URL on any device, enter the code, approve the permissions. The command
returns as soon as you approve.

> The code above is an example. Use the one your own terminal prints — codes are
> single-use and tied to one sign-in attempt.

Verify:

```bash
python3 scripts/todo.py auth status
python3 scripts/todo.py lists ls
```

---

## Evaluating without registering an app

Microsoft ships a multi-tenant public client with the Graph CLI and the Graph
PowerShell SDK: `14d82eec-204b-4c2f-b7e8-296a70dab67e`. It will work here.

Understand the trade-off before using it: consent and sign-in audit entries will
name *Microsoft Graph Command Line Tools*, not you; you cannot restrict its
scopes; and many organisations block or specifically audit it. Fine for a
five-minute look, wrong for anything ongoing.

---

## Where things are stored

| File | Contents | Mode |
| --- | --- | --- |
| `~/.config/ms-todo/config.json` | client id, tenant, timezone | 0600 |
| `~/.config/ms-todo/token.json` | access and refresh tokens | 0600 |
| `~/.config/ms-todo/lists-cache.json` | task list index, 5-minute TTL | 0600 |
| `~/.config/ms-todo/delta-state.json` | per-list delta tokens | 0600 |

Override the directory with `MSTODO_CONFIG_DIR`, or move the whole tree by
setting `XDG_CONFIG_HOME`. Files are written atomically, and the private mode is
applied to the temp file *before* the rename so the token is never briefly
world-readable.

`auth logout` deletes the token cache. To revoke access entirely — which
invalidates refresh tokens too — remove the app from
[account.live.com/consent/Manage](https://account.live.com/consent/Manage)
(personal) or **My Account → Privacy** (work or school).

---

## Environment variables

Every setting can come from the environment, which is what you want in CI:

| Variable | Meaning |
| --- | --- |
| `MSTODO_CLIENT_ID` | application (client) id |
| `MSTODO_TENANT` | `common`, `consumers`, `organizations`, or a tenant id |
| `MSTODO_CONFIG_DIR` | override the whole config directory |
| `MSTODO_TIMEZONE` | IANA zone for parsing and display |
| `MSTODO_TIMEOUT` | per-request timeout in seconds (default 30) |
| `MSTODO_RETRIES` | attempts per request including the first (default 5) |
| `MSTODO_SCOPES` | space-separated scope override |
| `MSTODO_AUTHORITY` | for sovereign clouds, e.g. `https://login.microsoftonline.us` |
| `MSTODO_GRAPH_BASE` | Graph base URL, e.g. `https://graph.microsoft.us/v1.0` |
| `NO_COLOR` | disable colour output |

Precedence is: command-line flag > environment variable > config file > default.

### Running unattended

The device code flow needs a human once. After that the refresh token keeps
working, so a cron job or CI runner only needs the config directory:

```bash
MSTODO_CONFIG_DIR=/opt/ms-todo-state python3 scripts/todo.py --json ls --overdue
```

Sign in once interactively with that same `MSTODO_CONFIG_DIR`, then let the job
run. Refresh tokens do eventually expire (inactivity, password change, admin
revocation, conditional-access policy), so handle exit code 3 by alerting a human
rather than retrying.

---

## Sovereign and national clouds

US Government L4 and L5 are supported by the To Do API; China (21Vianet) is not.
For GCC High:

```bash
export MSTODO_AUTHORITY=https://login.microsoftonline.us
export MSTODO_GRAPH_BASE=https://graph.microsoft.us/v1.0
```

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `invalid_client` at sign-in | *Allow public client flows* is off — step 2 above |
| `invalid_scope` | `Tasks.ReadWrite` was not added as a **delegated** permission |
| Exit code 6, "Access is denied" | Permission added but never consented; re-run `auth login`, or ask an admin to grant consent |
| Exit code 3 on a job that used to work | Refresh token expired or was revoked; sign in again interactively |
| `unknown time zone` | Slim Python image with no tz database: `pip install tzdata` |
| `no task lists found` | The mailbox has never opened To Do; open it once at [todo.microsoft.com](https://todo.microsoft.com) |
| Exit code 5 repeatedly | Graph is throttling the mailbox. Lower concurrency and back off; `--retries` already honours `Retry-After` |
| Dates land a day early or late | You passed `--due-tz`; drop it and let due dates default to midnight UTC |
