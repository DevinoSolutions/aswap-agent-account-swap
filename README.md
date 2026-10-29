# aswap: agent account swap for Claude Code and Codex

**Switch between multiple Claude Code and OpenAI Codex accounts automatically, before you hit a rate limit.**

`aswap` is a multi-account manager for AI coding agents. Save several Claude (Pro, Max 5x, Max 20x) and Codex logins, see every account's 5-hour and weekly usage in one place, and let `aswap auto` swap to the account with the most headroom the moment the current one runs low, so long agent sessions keep going instead of stopping at "usage limit reached".

- **Auto-switching**: watches usage live and switches accounts before the limit, not after.
- **Usage dashboard**: 5-hour, weekly and per-model windows for every account, in the terminal or a TUI.
- **Quota planning**: plan purchases, renewals and 5x→20x upgrades around weekly resets to get the most usage per month.
- **Crash-safe state**: atomic writes and hourly snapshots of your saved accounts (`aswap backup`).
- **Claude Code and Codex**: one tool for both CLIs, on Windows, macOS and Linux.

Built and maintained by [Devino Solutions](https://devino.ca).

Install it straight from GitHub; it is not published to PyPI.

## Installation

```bash
uv tool install git+https://github.com/DevinoSolutions/aswap-agent-account-swap
```

### From source

```bash
git clone https://github.com/DevinoSolutions/aswap-agent-account-swap.git
cd aswap-agent-account-swap
uv sync
uv tool install --force .
```

### Updating

```bash
uv tool install --force --reinstall git+https://github.com/DevinoSolutions/aswap-agent-account-swap
# or, from a checkout: git pull && uv tool install --force --reinstall .
```

Restart any running `aswap auto` afterwards; it keeps the old code until it exits.

Saved accounts, settings and logs live in `~/.claude-swap-backup/` (`$XDG_DATA_HOME/claude-swap` on Linux), so reinstalling or upgrading keeps every account.

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
aswap add
```

### Add more accounts

Log in with another account, then:

```bash
aswap add
```

With `aswap auto` (or the TUI) running, the `/login` alone is enough: the new account is added automatically once the login is stable (see [Automatic switching](#automatic-switching)).

### Switch accounts

Rotate to the next account:

```bash
aswap switch
```

Or switch to a specific account:

```bash
aswap switch 2
aswap switch user@example.com
aswap switch dev                # or by alias, once set with `aswap alias 2 dev`
```

Not sure which one? `aswap list` is the dashboard — every account's quota usage and reset times at a glance:

```bash
aswap list
```

Or aswap auto-picks by remaining quota — `aswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let aswap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
aswap auto                     # foreground loop, polls every 60s
aswap auto --threshold 80      # switch earlier
aswap auto --model Fable       # also switch when the Fable weekly limit is hit
aswap auto --once              # single check-and-switch, for cron/scripts
aswap auto --dry-run           # log what it would do, never switch
aswap auto --strategy consume-first   # burn the soonest-resetting account first

# One process can also keep five-hour windows started:
aswap config set autoswitch.warmupFiveHour true
aswap auto
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Strategies** (`--strategy`, or `aswap config set autoswitch.strategy`): `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- **The last point is used.** The usage API rounds up, so an account it calls 100% often still serves requests. For an account at 100% that matters to a decision (the active one, or a would-be target when everything else is full), auto sends a one-token request and reads the API's own rate-limit verdict from the response headers: still open, the account stays usable (ranked behind any account with measured headroom), and the active account is ridden until the gate closes, re-checked every few minutes; closed, it is treated as at its limit at once. Probes are throttled to one per account per 10 minutes (2.5 minutes for the active account while it rides the gate), at most 3 per check, never for a window that resets within 5 minutes, and a fresh statusline reading of the active account stands in for its probe for free. `aswap list`, the TUI and `--json` show the answer as e.g. `7d: 100% (open, gate 99%)` or `100% (blocked)`.
- Below the threshold, auto also moves when the active account is projected to hit 100% within 3 minutes. The projection uses the faster of the burn averaged over the last 10 minutes and the pace the latest readings prove (minus a rounding allowance), so a sudden jump in usage moves the work on the first reading that shows it instead of a minute later.
- A move made only because the active account is close to its limit must land somewhere that lasts meaningfully longer at the same burn rate (twice the runway, or five minutes more); otherwise auto rides the active account to its limit and switches then.
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `aswap add --slot N`, or replace its stored credentials from a known-good export — a plain `aswap import backup.aswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `aswap disable <num|email>`; `aswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `aswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `aswap switch <num|email>` target. They show a `(disabled)` marker in `aswap list`, in the [TUI](#interactive-dashboard-tui), and in the [menu bar](#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).
- **Manual switches are respected.** After you switch by hand (`aswap switch`, the TUI, an in-app `/login`), auto leaves that account alone for 60 minutes as far as its *optional* moves go — today that is the `consume-first` nudge. Moves that protect your session still happen: the account hitting its limit, crossing the threshold (or projected to hit 100% within minutes), or its usage becoming unreadable. The log says `Manual switch to N; holding it for 60 min unless it nears its limit`, `aswap status` and the TUI's Auto screen show `held until HH:MM`, a new hand switch restarts the hold, and an auto switch ends it. Change the window with `aswap config set autoswitch.manualHoldMinutes 30` (`0` turns it off); a running auto re-times the hold already in force rather than starting a new one. The hold is kept in the auto state file, so it survives an `aswap auto` restart.
- **Slots that need a login are kept.** When Claude Code reacts to a dead refresh token by wiping a session profile's credential, aswap marks that slot `(needs login)` in `aswap list`, the TUI and `aswap status`, skips it in auto-switch and bare `aswap switch`, and warns once (then hourly). Logging in again — in that pane, on the main login, or with `aswap add --slot N` — clears it on its own with one `logged in again` line.
- **New logins are added automatically.** While `aswap auto` (or the TUI) runs, a Claude Code `/login` with an account aswap does not manage yet is added to the next free slot — no `aswap add` — with one `Auto-added account N` log line; a re-login with a managed account whose stored credential is wiped, marked `(needs login)`, dead or older refreshes that slot the way `aswap add` would. It acts only once the login has looked identical for two polls in a row, confirms with the profile endpoint that the token really belongs to the account `~/.claude.json` names (a half-written `/login` is never mistaken for a new account), and writes under the same locks as a switch. OAuth logins only: API keys and setup-tokens still go through `aswap add-token`. Turn it off with `aswap config set autoswitch.autoAddAccounts false`.
- **A re-subscribed account is noticed within minutes.** An account cached as canceled, past due or free has its plan re-read from the profile endpoint as soon as it becomes active (any switch, or a new login for it) and every ~5 minutes in the background while `aswap auto` runs — independent of any usage-poll backoff. The flip is logged once, e.g. `Account 6 (…) is subscribed again: 5x`.
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `aswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
  - **Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `aswap list` (e.g. a line reading `Fable: 100%`).
- To take aswap out of the placement business entirely — because something else decides which account does what — run `aswap config set autoswitch.enabled false`. The engine then becomes **observe-only**: it still collects usage on its normal cadence, so `aswap watch`, the TUI and any external placer keep reading fresh numbers, but the active account is never changed, by any path. `aswap status` and `aswap list` say `placement: external (autoswitch.enabled=false)` so it is never a mystery, and going live from the TUI or the menu bar is refused with the same message. Default is `true`; nothing changes unless you set it.
- Five-hour warm-up is opt-in. Enable it with `aswap config set autoswitch.warmupFiveHour true`, or open the TUI's Auto screen (`aswap`, then `g`) and press `w`. The same `aswap auto` process then checks warm-up eligibility every ten minutes after making its switch decision. Do not also run the standalone `aswap warmup` loop; the shared lock prevents duplicate requests, but using one loop avoids needless contention and error messages.

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * aswap auto --once --json >> ~/.aswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `aswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)). A running `aswap auto` (or `aswap codex auto`) picks up a changed setting within one tick, without a restart, and logs one `settings reloaded: autoswitch.threshold 90 -> 80` line; a setting you passed as a flag keeps the flag's value until that process restarts. The TUI's Auto screen follows the change too.

</details>

### Keep five-hour windows warm (opt-in)

`aswap warmup` keeps stored OAuth accounts ready by checking usage every ten
minutes and sending one minimal Haiku request when an account has no live
five-hour window. It remains available for people who want warming without
automatic switching; otherwise enable `autoswitch.warmupFiveHour` and run only
`aswap auto`:

```bash
aswap warmup --once --dry-run   # inspect every decision; spend no quota
aswap warmup --once             # warm confirmed-cold accounts once
aswap warmup                    # keep checking in the foreground
aswap warmup --interval 900     # check every 15 minutes
```

This feature consumes real five-hour and weekly quota and is opt-in. It skips
disabled, API-key, non-switchable, and accounts with known exhausted weekly quota.
If usage remains missing, stale, errored, or hollow after a fresh probe, it
attempts one guarded request rather than leaving the window cold. Reliable
evidence of a live five-hour window still skips the request.
It never switches the global Claude login: other accounts use isolated session
profiles with customizations and tools disabled. Immediately before sending,
it verifies that the exact launch profile still uses the expected first-party
Claude OAuth identity; cloud-provider or changed-account profiles are skipped.
Successful and potentially accepted warms are recorded in `warmup_state.json`,
preventing an unavailable or stale usage snapshot from becoming a request on
every ten-minute poll, and preventing duplicates from a second warmer process.
The account lock stays held for each short warm request, so a simultaneous
`aswap switch` may wait or time out instead of racing the request onto the wrong
login.

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
aswap run 2                     # launch Claude Code as account 2, here only
aswap run user@example.com      # by email
aswap run 2 -- --resume         # everything after '--' is forwarded to claude
aswap run 2 --share-history     # share your chat history with this account too
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

<details>
<summary>Sharing details — MCP servers & chat history</summary>

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

</details>

<details>
<summary>Map accounts to directories — auto-pick per repo</summary>

Bind a directory to an account, and a bare `aswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
aswap map 2 ~/work/client-app   # map a directory to account 2
aswap map user@example.com      # map the current directory
aswap map                       # list mappings
aswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
aswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `aswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `aswap export`) and are cleaned up when their account is removed.

</details>

### Inspect session profiles

Each account's session profile is a real `CLAUDE_CONFIG_DIR` under the backup root, so `aswap profile` can tell you where it is and what state it is in — useful when another launcher points Claude Code at it directly.

```bash
aswap profile path 2               # just the directory, so it composes in a shell
aswap profile status 2             # hydrated? live sessions? identity drifted?
aswap profile status --all --json
aswap profile sync --all           # adopt a profile's rotated token into the store
aswap profile hydrate --all        # make every eligible profile logged in
aswap profile dehydrate 2          # drop its credentials, keep its history
```

`path` and `status` only ever report booleans, counts, paths and a token-expiry timestamp — never credential material.

`hydrate` prepares a profile so Claude Code finds a login in it, without launching anything; it is idempotent, skips disabled/API-key/empty slots, and one failing account never aborts a `--all` sweep. `dehydrate` removes a profile's credential material (history is kept) and refuses a profile that is live — or whose session records could not be read, which is not the same as nobody being there — unless you pass `--force`.

By default a profile is dehydrated whenever the stored credential changes, so the next launch re-bootstraps from the fresh copy. Set `aswap config set profiles.persistent true` to keep profiles logged in across an ordinary token rotation instead: aswap then re-seeds a quiescent profile from the blob it just wrote and leaves a live one alone. A re-login, an import, a switch-time backup or a slot move still re-bootstraps the profile — those genuinely change the identity or the token family. The default is `false`, which is exactly today's behaviour.

`sync` exists because Claude Code rotates its OAuth token *in place*, inside whatever config directory it was given. Once a session has run, its profile — not the stored backup — holds the newest generation of that account's token family, and the stored copy becomes a spent grant the server rejects forever. `sync` copies the profile's newer credential into the store: it sends nothing, consumes no grant and spends no quota, and it refuses a profile that is unreadable, half-written, wiped by a failed refresh, stale-marked, logged in as a different account, or simply not newer. aswap runs the same pass automatically before every usage collection, so ordinary use is already covered; the command is there for a launcher that wants to reconcile on its own schedule.

That automatic pass is **always on** — it is not gated behind `profiles.persistent`. The in-place rotation it repairs happens to anyone who runs `aswap run` (or points `CLAUDE_CONFIG_DIR` at a profile), whatever their settings say, and a slot whose stored grant has been superseded is an account that will eventually be marked `relogin_required` for no reason. The pass is cheap and deliberately timid: every refusal above, and a `lock-busy` when another aswap holds the store lock, leaves both the store and the profile byte-for-byte as they were. Nothing is written unless a strictly newer credential of the same identity and token family was read cleanly.

### Interactive dashboard (TUI)

Run `aswap` on its own (or `aswap tui`) for the full-screen dashboard: Claude Code and Codex appear together in labelled sections, with live usage, provider-correct switching, and auto-switching, all keyboard-driven. Arrow-key and Vim-style menu navigation wraps at both ends. The menu's **Settings…** screen cycles the theme, dashboard view, auto-switch threshold, and strategy; its dashboard view can show both providers or only Claude Code / Codex without stopping background updates for the hidden provider. `aswap watch` opens straight into the live monitor using the selected dashboard view. Works on macOS, Linux, and Windows.

<img src="assets/tui-watch.png" width="760" alt="aswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

### Codex accounts

`aswap` can also save and switch Codex CLI logins. Log into each account with `codex login`, then save it before logging into the next one:

```bash
codex login
aswap codex add

# Log in to the next Codex account, then save it too.
codex login
aswap codex add

aswap codex list                 # accounts tagged by plan, e.g. [Codex Team]
aswap codex usage                # Weekly usage plus banked-reset count/expiry
aswap codex switch 1
aswap codex switch                 # rotate to the next saved account
aswap codex auto --once            # switch when active quota reaches the threshold
aswap codex remove 2
```

Codex switching preserves the rest of `CODEX_HOME` (configuration, skills, sessions, and history) and replaces only `auth.json`. Restart Codex after switching so its running process loads the selected login. For ChatGPT-backed file logins, the dashboard reads the same read-only Codex rate-limit endpoints used by Codex and shows the weekly window with a live reset countdown, plus the number of banked resets and the earliest available reset's expiry. Window labels are derived from the duration reported by Codex, so older accounts that still return a 5-hour window remain correctly labelled. Each account is labelled by its ChatGPT plan (`Codex Team`, `Codex Pro`, `Codex Plus`, …) — read from the login token, the closest local equivalent to Claude Code's org name, since Codex stores no workspace name on disk. API-key accounts have no ChatGPT subscription quota, so they remain status-only. `aswap codex auto` uses the same threshold, cooldown, `--once`, `--dry-run`, and JSON event controls as Claude auto-switching; it prepares the account for the next Codex launch and reports when a restart is needed. That restart is for the Codex CLI only: `aswap codex auto` itself never needs one for a settings change, and picks up `aswap config set` within one tick exactly like `aswap auto` (see [Configuration](#configuration)).

Codex must use its documented file credential store. If your `~/.codex/config.toml` says `cli_auth_credentials_store = "keyring"`, change it to `"file"`, run `codex login`, then add the account. This deliberate restriction avoids writing a guessed OS-keyring entry.

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
aswap add
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
aswap run 2                     # Run an account in this terminal only (session mode)
aswap auto                      # Auto-switch when nearing rate limits (see above)
aswap warmup                    # Keep five-hour windows ready (opt-in quota spend)
aswap codex list                # List saved Codex CLI accounts
aswap codex switch 2            # Switch the file-backed Codex login
aswap config                    # Show or edit settings (see Configuration below)
aswap list                      # Claude + Codex accounts with quota and reset times
aswap list --provider codex     # Restrict to one provider (claude|codex|all)
aswap list --token-status       # Add source-labelled OAuth token diagnostics
aswap status                    # Show current account
aswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
aswap add --alias dev           # Add account and give it a short alias
aswap remove 2                  # Remove an account
aswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
aswap enable 2                  # Return a disabled account to rotation
aswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
aswap alias 2 --unset           # Remove an account's alias
aswap alias                     # List all aliases
aswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
aswap renumber                  # Renumber every account to 1..N (see "Account numbers" below)
aswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
aswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `aswap add`)
aswap tui                       # Interactive dashboard (also: bare `aswap`)
aswap watch                     # Dashboard, opened on the live watch page
aswap upgrade                   # Upgrade aswap to the latest version
aswap purge                     # Remove all aswap data
```

### Account numbers

A new account takes the lowest free number, so removing account 2 and adding another puts the new one at 2. A reused number starts clean: it does not inherit the removed account's alias, cached usage, gate or billing data, quarantine, manual hold or session profile.

Numbers can still drift, for example after removing accounts added before this behaviour, or when two machines added the same accounts in a different order. `aswap renumber` packs them back to 1..N:

```bash
aswap renumber --dry-run        # Show old -> new; change nothing
aswap renumber                  # Keep the current list order and close the gaps (asks first)
aswap renumber --by-email       # Order by email, then organization: same numbers on every machine
aswap renumber --yes --json     # Non-interactive; --json needs --yes or --dry-run
```

Everything that belongs to an account moves with it: credentials and config backups, alias, session profile, cached usage, gate and billing data, and the auto-switch state (quarantine, manual hold, last switch). Each step is an ordinary `aswap move` that commits on its own, and a snapshot of `state.db` is taken first. If the run is interrupted, the list is still consistent, and running `aswap renumber` again finishes it. The printed snapshot stamp undoes the whole run: `aswap backup restore --all --at STAMP`. Codex accounts have their own numbering and are not renumbered.

A running `aswap auto` (or the TUI) can stay up. Renumbering waits for any switch or token refresh in progress, and the engine follows the new numbers on its next poll. A switch it decided on the old numbers is refused rather than sent to whoever now holds that number (`no switch: roster-changed`), and the next poll decides again. `aswap renumber` refuses to run while an `aswap run` session is live, because that session's profile would move under it.

Safe sequence on a machine in daily use:

```bash
aswap renumber --by-email --dry-run   # check the mapping
aswap renumber --by-email             # confirm; note the snapshot stamp it prints
aswap list                            # same accounts, numbered 1..N; the active one unchanged
```

## Tips

- **Do you need to restart after switching?** For **Claude Code**, usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Codex always needs a restart.** Unlike Claude Code, the Codex CLI reads `auth.json` once when it starts and keeps the login in memory — it never re-reads the file or caches it on a timer. A running Codex session therefore keeps using the old account no matter what you swap underneath it. `aswap codex switch` and `aswap codex auto` write the selected login and tailor the reminder to your machine: if a Codex process is running they tell you to quit and relaunch it (or start a new session / reload the IDE extension); if none is running they confirm the account is ready for the next launch. This is a limitation of the Codex CLI, not aswap — there is no flag, signal, or config setting that makes a live Codex reload credentials, and aswap deliberately does not kill your running Codex process for you.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `aswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up provider credentials when you add an account
- Swaps only the account-specific Claude login (or Codex's file-backed
  `auth.json`) when you switch accounts; live account-independent OAuth state
  (such as MCP server logins) is preserved instead of being overwritten by a
  slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by re-adding it with `aswap add --slot N`, or by replacing its stored credentials from a known-good export — a plain `aswap import backup.aswap` replaces dead-token slots automatically)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping aswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.
- Codex usage checks refresh inactive saved logins when possible; running Codex sessions must be restarted after a Codex account switch

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`aswap run`) live under the backup directory in `sessions/`. The roster, credential and config backups, tool preferences (`settings.json`), auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts) and five-hour warmer state (`warmup_state.json`) are documents in one crash-safe SQLite database, `state.db`, in the backup directory root; `aswap backup export DIR` writes them all out as files.

The first run of a version with `state.db` imports the JSON files of an older aswap and renames each to `<name>.migrated`, which it never writes again. Stop `aswap auto`, the dashboard and the menu bar before upgrading: an older aswap still running keeps writing the JSON files, and they win over the database copy at the next import. To go back to an older aswap, stop every aswap process, run `aswap backup export <backup directory>` (it writes the current state back out under the old file names), then install the older version.

`aswap backup take|list|check|restore` manage hourly snapshots of `state.db` under `snapshots/` (taken with SQLite's online backup, so they are consistent while other aswap processes write).

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Menu bar (macOS)

<details>
<summary>Optional macOS menu bar app — usage at a glance, click to switch</summary>

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'aswap[menubar] @ git+https://github.com/DevinoSolutions/aswap-agent-account-swap'
aswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as [`aswap auto`](#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on.

</details>

## Advanced

### Configuration

Tool preferences are the `settings.json` document in the backup root's `state.db`; `aswap config` reads and edits them with validation, so you never have to guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
aswap config                              # list effective settings ("(default)" = not set)
aswap config get autoswitch.threshold
aswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
aswap config set ui.view codex             # combined (default), claude, or codex
aswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
aswap config set profiles.persistent true # keep session profiles logged in across rotations
aswap config set autoswitch.enabled false # observe only: never change the active account
aswap config unset autoswitch.threshold   # back to the default
aswap config path                         # where the settings are stored
aswap config export settings.json         # write them out (no FILE: stdout)
aswap config import settings.json         # validate strictly, then replace them
```

`aswap config --help` lists every key with its valid range and default. To hand-edit, export, edit the file, and import it back: the import rejects unknown keys and out-of-range or mistyped values before writing anything. `list` and `get` take `--json` for scripting.

**Changes apply to a running `aswap auto` or `aswap codex auto` within one tick** (the poll interval, 60 s by default) — no restart. The engine checks the settings document's last-write time each tick and while it sleeps, applies the changed keys from the next tick on and logs them as `settings reloaded: <key> <old> -> <new>` (`--json`: a `settings-reloaded` event). What it already knows is kept: a manual hold, the cooldown, quarantines and the burn-rate history carry over. Keys given as flags (`--threshold`, `--interval`, `--cooldown`, and for `aswap auto` also `--model`, `--strategy`, `--include-api-key-accounts`) keep the flag's value until that process restarts, as does a threshold set for the session in the TUI. A stored value that fails validation (a bad hand edit) is never applied: the running engine keeps its last good value and logs a warning. The TUI's Auto screen (Claude or Codex) updates its summary line — threshold, poll interval, warm-up — when its engine applies a change.

</details>

### Backup and migration

Move account data between machines or back it up:

```bash
aswap export backup.aswap                    # All accounts to a file
aswap export backup.aswap --account 2        # One account
aswap export backup.aswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
aswap import backup.aswap                    # Skips accounts that already exist
aswap import backup.aswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `aswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `aswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
aswap list --json --profiles        # ...plus each account's session-profile state
aswap list --json                   # BOTH providers, merged (default)
aswap list --provider claude --json # Claude Code accounts only (bare payload)
aswap list --provider codex --json  # Codex accounts only (bare payload)
aswap status --json                 # current active account
aswap switch --strategy best --json # switch, then report the result
aswap switch 2 --json
```

`aswap list` now covers both providers by default. In `--json` mode that means the output is keyed by provider (`claude`/`codex`); pass `--provider claude` (or `codex`) to get a single provider's **bare** payload — the same shape older scripts already parse.

<details>
<summary>Example output & schema notes</summary>

Merged (default) — `aswap list --json`:

```json
{
  "claude": {
    "schemaVersion": 1,
    "activeAccountNumber": 2,
    "accounts": [
      { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
        "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                   "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
    ]
  },
  "codex": {
    "provider": "codex",
    "activeAccountNumber": 1,
    "accounts": [
      { "number": 1, "email": "you@example.com", "authMode": "chatgpt",
        "planType": "team", "label": "Codex Team", "active": true }
    ]
  }
}
```

Single provider — `aswap list --provider claude --json` — returns just the inner Claude object above (`schemaVersion`, `activeAccountNumber`, `accounts`), unchanged from before. **Migration:** scripts that parsed the old top-level `accounts` from `aswap list --json` should switch to `aswap list --provider claude --json`.

Every Claude payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `aswap disable` carries an additive `"disabled": true` on its row (absent otherwise).

An account row also carries an additive `alias` field once one is set with `aswap alias` (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Three more additive fields are always present on a Claude list row, for launchers that place work across accounts:

- `profilePath` — the session-profile directory for that slot (see [session profiles](#inspect-session-profiles)), emitted whether or not anything is hydrated there;
- `eligible` / `ineligibleReason` — whether work may be routed to this slot, and why not (`disabled`, `api_key`, `relogin_required`, `no_credentials`, `foreign_credential`, `keychain_unavailable`). Transient states (`token_expired`, `unavailable`) stay eligible: the slot is fine, that poll was not.

The payload also carries a top-level `usageCache` object (`serveTtlSeconds`, `staleOkSeconds`) so a script can judge how stale a `usageFetchedAt` is without hardcoding aswap's constants. When a number is not fresh enough, `aswap usage refresh --max-age 60 --json` forces a poll — it calls the same free OAuth usage-metadata endpoint every normal poll uses and never spends paid tokens. It will not fetch a row younger than 30s, allows one forced sweep per 30s across all processes, and still honours dead-token quarantines and 429 backoffs, so it is an escape hatch from the cadence and not from the request budget.

`aswap list --json --profiles` adds one opt-in block per row — `profileHydrated`, `profileLiveSessions`, `profileUnreadableRecords`, `profileIdentityDrifted`, `profileTokenExpiresAt`. It costs a small read and a PID scan per account, which is why it is not on by default. It reports state only; no credential material ever appears in aswap's JSON.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

</details>

`aswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `settings-reloaded`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
aswap add-token sk-ant-oat01-...             # OAuth setup-token
aswap add-token sk-ant-api03-...             # managed API key
aswap add-token sk-ant-oat01-... --slot 3
aswap add-token - --slot 3                   # read token from stdin
aswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

### Endpoint overrides

aswap talks to `api.anthropic.com` (usage, profile, the rate-limit probe), `platform.claude.com` (token refresh) and `claude.ai` (`aswap billing`). Two environment variables point those requests somewhere else, e.g. a local stand-in server for testing:

```bash
ASWAP_API_BASE_URL=http://127.0.0.1:8765 aswap list      # api.anthropic.com + platform.claude.com
ASWAP_CLAUDE_AI_BASE_URL=http://127.0.0.1:8765 aswap billing status   # claude.ai
```

The value must be an `https://` URL, or `http://` to `localhost`/`127.0.0.1`/`::1` only; anything else is refused with exit code 2 before a request is sent, because these requests carry your account tokens. Unset means the real hosts. (`ASWAP_TIME_SCALE`, which shortens polling intervals, exists for the end-to-end tests and only applies while `ASWAP_API_BASE_URL` is a loopback address; see [CONTRIBUTING.md](CONTRIBUTING.md).)

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
aswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall aswap
# or
pipx uninstall aswap
```

## Requirements

- Python 3.12+
- Claude Code and/or Codex CLI
- A file-backed login for managed Codex accounts

## Contributing

Unit tests run with `uv run pytest`; the end-to-end tier, which drives the real `aswap` binary against a fake Claude server, runs with `uv run pytest -m e2e tests/e2e`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GNU Affero General Public License v3.0 or later; see [LICENSE](LICENSE). If you run a modified aswap for other people, including as a network service, you must offer them its source under the same license.

aswap started as a fork of an MIT-licensed project; the code that came from it keeps its MIT notice in [LICENSE-MIT](LICENSE-MIT).
