"""Command-line interface for Claude Swap."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from claude_swap import __version__, endpoints, paths, printer, statedb
from claude_swap.codex import CodexAccountSwitcher
from claude_swap.exceptions import ClaudeSwitchError, ConfigError, ValidationError
from claude_swap.json_output import error_envelope
from claude_swap.printer import (
    accent,
    bolded,
    dimmed,
    error,
    force_utf8_output,
    muted,
    warning,
    yellowed,
)
from claude_swap.settings import load_ui_settings
from claude_swap.switcher import ClaudeAccountSwitcher


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\aswap``). We strip that down to the
    bare command the user typed (``aswap`` / ``aswap``), falling back to
    ``aswap`` for ``python -m claude_swap`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "aswap"
    return name


# Memorable subcommand aliases → the long-standing flags they expand to. Lets
# users type `aswap list`, `aswap status`, `aswap add`, etc. instead of `--list`
# / `--status` / `--add-account`, which all still work. `switch` is special-cased
# below (a bare `switch` rotates; `switch <target>` jumps to one account) and
# `run`/`auto` keep their own pre-dispatch parsers, so none of those are listed here.
_SUBCOMMAND_FLAGS = {
    "help": "--help",
    "list": "--list",
    "ls": "--list",
    "status": "--status",
    "add": "--add-account",
    "add-token": "--add-token",
    "remove": "--remove-account",
    "rm": "--remove-account",
    "disable": "--disable-account",
    "enable": "--enable-account",
    "export": "--export",
    "import": "--import",
    "purge": "--purge",
    "upgrade": "--upgrade",
    "update": "--upgrade",
    "tui": "--tui",
    "watch": "--watch",
    "menubar": "--menubar",
}


def _translate_subcommand(argv: list[str]) -> list[str]:
    """Rewrite a leading memorable subcommand into the equivalent flag argv.

    ``argv`` is the args after the program name. The rewrite only fires when the
    first token is a recognized verb (which never starts with '-'), so the
    established ``--flag`` interface — and every existing test that drives it —
    is left untouched. Tokens after the verb pass through verbatim, so flags
    like ``--json``, ``--strategy``, ``--slot``, and ``--force`` keep combining
    exactly as before (e.g. ``aswap switch --strategy best``, ``aswap list --json``).
    """
    if not argv:
        return argv

    verb, rest = argv[0], argv[1:]

    if verb == "switch":
        # Bare `switch` rotates; `switch <num|email>` jumps to that account.
        if rest and not rest[0].startswith("-"):
            return ["--switch-to", *rest]
        return ["--switch", *rest]

    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag is not None:
        return [flag, *rest]

    return argv


def _run_command(argv: list[str]) -> None:
    """Handle `aswap run NUM|EMAIL [--no-share] [-- <claude args>]`.

    Pre-dispatched before the main parser is built: a positional subcommand
    can't coexist with main()'s mutually-exclusive flag group, and this keeps
    the existing parser untouched. Limitation: `run` must be the
    first argument (`aswap --debug run 2` is not supported; use
    `aswap run 2 --debug`).

    On POSIX this execs claude and never returns; on Windows it exits with
    claude's return code. Either way the post-dispatch update check in
    main() is unreachable, which is intended.
    """
    # Everything after the first `--` is forwarded to claude verbatim.
    if "--" in argv:
        split = argv.index("--")
        head, tail = argv[:split], argv[split + 1 :]
    else:
        head, tail = argv, []

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} run",
        description=(
            "[EXPERIMENTAL] Launch Claude Code as a stored account in this "
            "terminal only (the default login and other terminals are "
            "unaffected)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap run 2
  aswap run user@example.com
  aswap run 2 --no-share
  aswap run 2 --share-history
  aswap run 2 -- --resume
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to run (number or email). Omit to use the current "
        "directory's mapping (see `aswap map`).",
    )
    parser.add_argument(
        "--no-share",
        action="store_true",
        help=(
            "Don't share settings/keybindings/CLAUDE.md/skills/commands/agents "
            "from ~/.claude into the session profile (and remove previously "
            "shared items)"
        ),
    )
    parser.add_argument(
        "--share-history",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Share conversation history (projects/ and history.jsonl) from "
            "~/.claude into the session profile, so every account sees one "
            "unified history. History the profile already accumulated is "
            "merged into ~/.claude first. --no-share-history restores "
            "per-account history (the default). Not supported on Windows."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(head)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.session import SessionManager

        manager = SessionManager(switcher)

        if args.account is not None:
            manager.run(
                args.account,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
            )
            return  # only reachable in tests where exec/exit is mocked

        # No account given: resolve from the current directory's mapping.
        slot, email = switcher.slot_for_directory(os.getcwd())
        if slot is not None:
            manager.run(
                slot,
                tail,
                share=not args.no_share,
                share_history=args.share_history,
            )
            return  # only reachable in tests
        if email is not None:
            warning(
                f"Mapped account {email} no longer exists — "
                "launching the default account."
            )
        else:
            print(
                dimmed(
                    f"No account mapped for {os.getcwd()} — "
                    "launching the default account."
                )
            )
        manager.exec_default(tail)
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _guard_root(switcher: ClaudeAccountSwitcher) -> None:
    """Refuse to run as root outside a container (shared by run/map/unmap)."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _map_command(argv: list[str]) -> None:
    """Handle `aswap map [NUM|EMAIL] [PATH]`.

    With no NUM|EMAIL, lists all mappings. Otherwise maps PATH (default: the
    current directory) to the given account. Pre-dispatched before the main
    parser for the same reason as `run` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} map",
        description=(
            "Map a stored account to a directory so `aswap run` (with no "
            "account) auto-launches it there. With no arguments, lists all "
            "mappings."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap map 2 ~/work/client-app
  aswap map user@example.com          # map the current directory
  aswap map                           # list all mappings
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to map (number or email). Omit to list mappings.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to map (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            switcher.list_mappings()
            return

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        account_num, email, org_uuid = switcher.resolve_account(args.account)
        target = args.path or os.getcwd()
        if not os.path.isdir(target):
            warning(f"Warning: {target} is not an existing directory (mapping it anyway)")
        previous = store.get(target)
        store.set(target, email, org_uuid)

        shown = normalize_path(target)
        if previous and previous.get("email") != email:
            prev_email = previous.get("email")
            print(
                f"{accent('Mapped')} {shown} → Account-{account_num} ({email}) "
                f"{muted(f'(was {prev_email})')}"
            )
        else:
            print(f"{accent('Mapped')} {shown} → Account-{account_num} ({email})")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unmap_command(argv: list[str]) -> None:
    """Handle `aswap unmap [PATH]` — remove a directory→account mapping."""
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unmap",
        description="Remove a directory → account mapping (default: current directory).",
    )
    parser.add_argument(
        "path",
        nargs="?",
        metavar="PATH",
        help="Directory to unmap (default: current directory)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        from claude_swap.mappings import MappingStore, normalize_path

        store = MappingStore(switcher.backup_dir)
        target = args.path or os.getcwd()
        shown = normalize_path(target)
        if store.remove(target):
            print(f"{accent('Unmapped')} {shown}")
        else:
            print(dimmed(f"No mapping for {shown}"))
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _unclaimed_command(argv: list[str]) -> None:
    """Handle `aswap unclaimed [--purge ID]` — inspect or drop a stash row.

    The stash holds credential bytes a switch or a consume gate could not
    attribute to a slot. Rows normally clear themselves (the next gate pass
    adopts or retires them), but two states need a human: a row whose bytes
    are unreadable until a keychain is unlocked or a mode is fixed, and one
    whose metadata was lost, which no pass can ever adopt. ``--json`` lists
    only bare ids, so without this there is nothing to look at and nothing to
    drop short of hand-editing the manifest.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List stashed credential entries, or purge one by id. "
            "Purging deletes the bytes — recovery is /login + `aswap add`."
        ),
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="Delete this entry's bytes and manifest row",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed entry {args.purge}")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Purged')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed credential entries"))
            return
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot") or "?"
            reason = meta.get("reason") or "orphaned (no manifest row)"
            print(f"{entry_id}  slot {slot}  {reason}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _swap_command(argv: list[str]) -> None:
    """Handle `aswap swap NUM|EMAIL|ALIAS NUM|EMAIL|ALIAS`.

    Exchanges the two accounts' slot numbers (list order and numeric
    targets). Pre-dispatched before the main parser for the same reason as
    `alias` (the main parser's required mutually-exclusive group can't hold
    a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' slot numbers, so they trade places in "
            "`aswap list` and as numeric targets. Aliases, backups, and "
            "session history move with their account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap swap 1 2
  aswap swap dev user@example.com
        """,
    )
    parser.add_argument("first", metavar="NUM|EMAIL|ALIAS", help="One account")
    parser.add_argument("second", metavar="NUM|EMAIL|ALIAS", help="The other account")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} Account {num_a} and Account {num_b}:")
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        for num in sorted((num_a, num_b), key=int):
            email = accounts.get(num, {}).get("email", "")
            print(f"  {num}: {email}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _move_command(argv: list[str]) -> None:
    """Handle `aswap move NUM|EMAIL|ALIAS SLOT`.

    Assigns an account to a specific slot number. If the slot is empty the
    account is relocated there (its old slot is freed); if it is occupied the
    two accounts trade places. `swap a b` is exactly `move a <b's slot>`.
    Pre-dispatched before the main parser for the same reason as `alias`.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Assign an account to a slot number. An empty slot relocates the "
            "account there and frees its old slot; an occupied slot swaps the "
            "two. Aliases, backups, and session history move with the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap move user@example.com 1   move an account onto shortcut 1
  aswap move dev 1                by alias
  aswap move 2 1                  by number (swaps if slot 1 is taken)
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL|ALIAS", help="Account to move")
    parser.add_argument("slot", metavar="SLOT", help="Destination slot number")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        data = switcher._get_sequence_data() or {}
        accounts = data.get("accounts", {})
        if num_src == num_target:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{dimmed('Already in')} slot {num_target}: {email}")
        elif swapped:
            print(f"{accent('Swapped')} Account {num_src} and Account {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                email = accounts.get(num, {}).get("email", "")
                print(f"  {num}: {email}")
        else:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{accent('Moved')} {email} to slot {num_target}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _priority_command(argv: list[str]) -> None:
    """Handle `aswap priority [NUM|EMAIL ...] [--clear]`.

    Sets ``autoswitch.priority``: the accounts auto uses up first, in order,
    over consume-first ordering, the manual hold and the cooldown. Stored as
    emails so the list survives `aswap renumber` and reads the same on every
    machine. Pre-dispatched before the main parser for the same reason as
    `alias`.
    """
    from claude_swap.settings import (
        load_settings,
        parse_priority,
        set_setting,
        unset_setting,
    )

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} priority",
        description=(
            "Pick accounts that aswap auto uses up first, in the order given. "
            "Auto moves onto the first one with room right away and stays on "
            "it until its limit, whatever resets sooner and whatever account "
            "was picked by hand; then it moves to the next one on the list."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap priority 3 5 7                use up accounts 3, 5 and 7 first
  aswap priority user@example.com     by email works too
  aswap priority                      show the list
  aswap priority --clear              back to the normal strategy
        """,
    )
    parser.add_argument(
        "accounts", nargs="*", metavar="NUM|EMAIL",
        help="Accounts in priority order. Omit to show the list.",
    )
    parser.add_argument("--clear", action="store_true", help="Remove the priority list")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)
    if args.clear and args.accounts:
        parser.error("--clear does not take accounts")

    root = paths.get_backup_root()
    try:
        if args.clear:
            if unset_setting(root, "autoswitch.priority"):
                print(f"{accent('Cleared')} the priority list")
            else:
                print(dimmed("No priority list was set"))
            return
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        if not args.accounts:
            names = parse_priority(load_settings(root).priority)
            if not names:
                print(dimmed("No priority accounts; auto follows autoswitch.strategy"))
                return
            print(bolded("Priority (used up first, in order):"))
            for i, name in enumerate(names, 1):
                try:
                    num, email, _org = switcher.resolve_account(name)
                except ClaudeSwitchError:
                    print(f"  {i}. {name} {muted('(no such account here)')}")
                    continue
                print(f"  {i}. Account {num} {muted(f'({email})')}")
            return
        chosen: list[str] = []
        shown: list[str] = []
        for ident in args.accounts:
            num, email, _org = switcher.resolve_account(ident)
            entry = email or num
            if entry.lower() not in (c.lower() for c in chosen):
                chosen.append(entry)
                shown.append(num)
        set_setting(root, "autoswitch.priority", ",".join(chosen))
        print(
            f"{accent('Priority set')}: account "
            + ", ".join(shown)
            + muted(" (a running aswap auto picks it up on its next check)")
        )
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)


def _renumber_command(argv: list[str]) -> None:
    """Handle `aswap renumber [--by-email] [--dry-run] [--yes] [--json]`.

    Packs every Claude account's slot number to 1..N with the same machinery
    as `aswap move`. Pre-dispatched before the main parser for the same
    reason as `alias`.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} renumber",
        description=(
            "Renumber every account to 1..N. By default the current list "
            "order is kept; --by-email orders by email (then organization), "
            "so two machines with the same accounts get the same numbers. "
            "Aliases, backups, session history, cached usage and the "
            "auto-switch state move with each account, a safety snapshot is "
            "taken first, and a running `aswap auto` keeps working. Codex "
            "accounts are not renumbered."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap renumber --dry-run        show old -> new, change nothing
  aswap renumber                  keep list order, close the gaps
  aswap renumber --by-email --yes same numbering on every machine
        """,
    )
    parser.add_argument(
        "--by-email",
        action="store_true",
        help="Order by email, then organization (stable across machines)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the mapping; change nothing"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Do not ask for confirmation"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)
    json_mode = args.json

    def show_mapping(rows: list[dict]) -> None:
        width = max((len(str(r["from"])) for r in rows), default=1)
        for row in rows:
            old, new = row["from"], row["to"]
            label = f"  {str(old).rjust(width)} -> {new}: {row['email']}"
            print(label if old != new else dimmed(label))

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        with _json_stdout_guard(json_mode):
            preview = switcher.renumber_preview(by_email=args.by_email)
        count = len(preview["accounts"])

        if not preview["changed"] or args.dry_run:
            if json_mode:
                print(json.dumps(
                    {"schemaVersion": 1, "dryRun": bool(args.dry_run),
                     "renumbered": False, "snapshot": None, **preview},
                    indent=2,
                ))
                return
            if not preview["changed"]:
                print(f"{dimmed('Already numbered')} 1..{count}; nothing to do.")
                return
            print(f"Would renumber {count} account(s) "
                  f"({len(preview['moves'])} move(s)):")
            show_mapping(preview["accounts"])
            return

        if not args.yes:
            if json_mode:
                raise ValidationError(
                    "renumber --json needs --yes (or --dry-run): it cannot prompt"
                )
            print(f"Renumber {count} account(s) to 1..{count} "
                  f"({len(preview['moves'])} move(s)):")
            show_mapping(preview["accounts"])
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{dimmed('Cancelled')}")
                return
            if answer not in ("y", "yes"):
                print(dimmed("Cancelled"))
                return

        with _json_stdout_guard(json_mode):
            result = switcher.renumber_accounts(
                by_email=args.by_email, expected=preview["accounts"]
            )
        if json_mode:
            print(json.dumps(
                {"schemaVersion": 1, "dryRun": False,
                 "renumbered": bool(result["changed"]), **result},
                indent=2,
            ))
            return
        print(f"{accent('Renumbered')} {count} account(s) to 1..{count}:")
        show_mapping(result["accounts"])
        if result.get("snapshot"):
            print(dimmed(
                f"Snapshot {result['snapshot']} holds the old numbering "
                f"(undo: aswap backup restore --all --at {result['snapshot']})"
            ))
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _alias_command(argv: list[str]) -> None:
    """Handle `aswap alias [NUM|EMAIL] [NAME] [--unset]`.

    With no arguments, lists all aliases. Otherwise sets (or, with --unset,
    removes) the alias for the given account. Pre-dispatched before the main
    parser for the same reason as `map` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} alias",
        description=(
            "Set, remove, or list a short display alias for an account. "
            "Once set, the alias can be used anywhere an account number or "
            "email is accepted (switch, remove, run, map)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap alias 2 dev
  aswap alias user@example.com dev
  aswap alias 2 --unset
  aswap alias                         # list all aliases
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to alias (number or email). Omit to list aliases.",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="Alias to set (letters, digits, ., -, _; not purely numeric).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            rows = switcher.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, alias_name, email in rows:
                print(f"  {num}: {alias_name} {muted(f'({email})')}")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {account_num}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {account_num}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _codex_command(argv: list[str]) -> None:
    """Handle ``aswap codex`` account management commands."""
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} codex",
        description=(
            "Manage file-backed Codex CLI accounts. Run 'codex login' for an "
            "account, then use 'aswap codex add' to save it."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    subcommands = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("list", "List managed Codex accounts"),
        ("status", "Show active Codex account"),
        ("usage", "Fetch Codex subscription usage and diagnostic errors"),
    ):
        command = subcommands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true")
    add = subcommands.add_parser("add", help="Save the current Codex login")
    add.add_argument("--slot", type=int, metavar="N")
    switch = subcommands.add_parser("switch", help="Switch to the next or named Codex account")
    switch.add_argument("account", nargs="?", metavar="NUM|EMAIL")
    switch.add_argument("--json", action="store_true")
    remove = subcommands.add_parser("remove", help="Remove a saved Codex account")
    remove.add_argument("account", metavar="NUM|EMAIL")
    remove.add_argument("--yes", action="store_true", help="Skip confirmation")
    auto = subcommands.add_parser(
        "auto",
        help="Switch Codex accounts when the active quota nears its limit",
        description=(
            "Watch Codex subscription usage and select a better saved account. "
            "A running Codex session must be restarted after a switch. "
            "Defaults come from `aswap config` (autoswitch.*); a running loop "
            "picks up `aswap config set` changes within one tick, no restart, "
            "while a setting given as a flag keeps its flag value until restart."
        ),
    )
    auto.add_argument("--once", action="store_true", help="Evaluate once and exit")
    auto.add_argument("--json", action="store_true", help="Emit JSONL events")
    auto.add_argument("--interval", type=float, metavar="SECONDS")
    auto.add_argument("--threshold", type=float, metavar="PCT")
    auto.add_argument("--cooldown", type=float, metavar="SECONDS")
    auto.add_argument("--dry-run", action="store_true", help="Report decisions without switching")
    args = parser.parse_args(argv)

    try:
        switcher = CodexAccountSwitcher(debug=args.debug)
        if args.action == "list":
            switcher.list_accounts(json_output=args.json)
        elif args.action == "status":
            switcher.status(json_output=args.json)
        elif args.action == "usage":
            switcher.usage_status(json_output=args.json)
        elif args.action == "add":
            switcher.add_account(slot=args.slot)
        elif args.action == "remove":
            switcher.remove_account(args.account, assume_yes=args.yes)
        elif args.action == "auto":
            import signal
            import time as _time

            from claude_swap.codex_autoswitch import CodexAutoSwitchEngine
            from claude_swap.printer import accent, yellowed
            from claude_swap.settings import (
                cli_overrides,
                load_settings,
                merged_with_cli,
            )

            def emit(event) -> None:
                if args.json:
                    print(json.dumps(event.to_json()), flush=True)
                    return
                stamp = _time.strftime("%H:%M:%S")
                line = event.human()
                if event.kind == "switch":
                    line = accent(line)
                elif event.kind == "error":
                    line = yellowed(line)
                else:
                    line = dimmed(line)
                print(f"{stamp}  {line}", flush=True)

            settings = merged_with_cli(load_settings(switcher.backup_dir), args)
            engine = CodexAutoSwitchEngine(
                switcher,
                settings,
                emit,
                dry_run=args.dry_run,
                # Flags hold for the life of the process; every other key
                # follows `aswap config` changes from the next tick on.
                pinned=cli_overrides(args),
            )
            if args.once:
                sys.exit(engine.tick().value)
            if not args.json:
                print(
                    dimmed(
                        f"Codex auto-switch running: threshold {settings.threshold:.0f}%, "
                        f"every {settings.interval_seconds:.0f}s"
                        f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                    )
                )
            signal.signal(signal.SIGTERM, lambda *_: engine.stop())
            sys.exit(engine.run_loop())
        elif args.account:
            switcher.switch_to(args.account, json_output=args.json)
        else:
            switcher.switch(json_output=args.json)
    except ClaudeSwitchError as exc:
        error(f"Error: {exc}")
        sys.exit(1)


def _list_dispatch(claude_switcher: "ClaudeAccountSwitcher", args) -> dict | None:
    """Run ``list`` across providers, honoring ``--provider`` and ``--json``.

    A single provider in ``--json`` mode returns that provider's bare payload,
    so scripts on the old Claude-only ``list --json`` migrate by adding
    ``--provider claude``. ``all`` (the default) nests both under
    ``claude``/``codex`` keys. Human mode prints one section per provider and
    never triggers Claude's interactive first-run inside the merged view.
    """
    provider = args.provider
    if provider == "claude":
        return claude_switcher.list_accounts(
            show_token_status=args.token_status,
            json_output=args.json,
            include_profiles=args.profiles,
        )

    codex_switcher = CodexAccountSwitcher(debug=args.debug)
    if provider == "codex":
        if args.json:
            return codex_switcher.list_payload()
        codex_switcher.list_accounts(json_output=False)
        return None

    # provider == "all": merge both providers.
    if args.json:
        claude_payload = claude_switcher.list_accounts(
            show_token_status=args.token_status,
            json_output=True,
            include_profiles=args.profiles,
        )
        return {"claude": claude_payload, "codex": codex_switcher.list_payload()}

    print(bolded("Claude Code"))
    if statedb.exists(claude_switcher.sequence_file):
        claude_switcher.list_accounts(
            show_token_status=args.token_status, json_output=False
        )
    else:
        print(dimmed("  No Claude Code accounts."))
    print()
    print(bolded("Codex"))
    codex_switcher.list_accounts(json_output=False)
    return None


def _auto_command(argv: list[str]) -> None:
    """Handle `aswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built, like `run` (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  aswap auto                       # foreground loop, switch at 90%% used
  aswap auto --threshold 80        # switch earlier
  aswap auto --model Fable         # also switch when the Fable weekly limit is hit
  aswap auto --json                # one JSON event per line (for scripts)
  aswap auto --once; echo $?       # single tick, outcome in exit code
  aswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json in the backup root; flags override them.
A running loop picks up `aswap config set` changes within one tick, no
restart; a setting given as a flag keeps its flag value until restart.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first"),
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default) or "
            "'consume-first' (proactively use the account whose weekly window "
            "resets soonest)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from claude_swap.autoswitch import AutoSwitchEngine, AutoSwitchEvent
    from claude_swap.printer import accent, yellowed
    from claude_swap.settings import cli_overrides, load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined") or (
            event.kind == "warmup" and getattr(event, "action", None) == "failed"
        ):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        engine = AutoSwitchEngine(
            switcher,
            settings,
            jsonl_emit if args.json else human_emit,
            dry_run=args.dry_run,
            # Flags hold for the life of the process; every other key
            # follows `aswap config` changes from the next tick on.
            pinned=cli_overrides(args),
        )

        if settings.warmup_five_hour and not args.dry_run and not args.json:
            print(
                yellowed(
                    "Five-hour warm-up is enabled: cold or unconfirmed OAuth "
                    "accounts may receive one minimal Haiku request per window."
                )
            )

        if args.once:
            sys.exit(engine.tick().value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        if not args.json:
            print(
                dimmed(
                    f"Auto-switch running: threshold {settings.threshold:.0f}%, "
                    f"every {settings.interval_seconds:.0f}s"
                    f" · 5h warm-up {'on' if settings.warmup_five_hour else 'off'}"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        sys.exit(engine.run_loop())
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _warmup_command(argv: list[str]) -> None:
    """Handle ``aswap warmup`` without ever changing the default account."""
    import math
    import signal
    import time as _time

    from claude_swap.warmup import (
        DEFAULT_INTERVAL_SECONDS,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT_SECONDS,
        MIN_INTERVAL_SECONDS,
        WarmupEngine,
        WarmupEvent,
    )
    from claude_swap.printer import yellowed

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} warmup",
        description=(
            "Keep stored OAuth accounts' five-hour windows ready. A minimal "
            "Haiku request is sent when usage shows no live window, or once "
            "under a five-hour guard when usage remains unavailable. This "
            "consumes real five-hour and weekly quota."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap warmup                         # check every 10 minutes
  aswap warmup --interval 900          # check every 15 minutes
  aswap warmup --once                  # one check, then exit
  aswap warmup --once --dry-run        # show decisions; spend no quota

The globally active Claude login is never switched. Non-active accounts run
through aswap's isolated session profiles. Ctrl-C stops loop mode.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once, warm eligible accounts, and exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help="Seconds between checks in loop mode (min 300; default 600)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="Maximum time for each minimal Claude request (default 120)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and report only; never send a Claude request",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    if not math.isfinite(args.interval) or args.interval < MIN_INTERVAL_SECONDS:
        parser.error(f"--interval must be at least {MIN_INTERVAL_SECONDS:.0f} seconds")
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")

    def human_emit(event: WarmupEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        label = f"Account-{event.account_number} ({event.email})"
        line = f"{label}: {event.detail}"
        if event.kind in ("warmed", "would-warm"):
            line = accent(line)
        elif event.kind == "failed":
            line = yellowed(line)
        else:
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        engine = WarmupEngine(
            switcher,
            emit=human_emit,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout,
            model=DEFAULT_MODEL,
            dry_run=args.dry_run,
        )

        if args.once:
            if not args.dry_run:
                print(
                    yellowed(
                        "Warm-up sends real requests: each cold account uses "
                        "five-hour and weekly quota."
                    )
                )
            summary = engine.tick()
            sys.exit(1 if summary.failed else 0)

        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
        print(
            dimmed(
                f"Five-hour warm-up running every {args.interval:.0f}s with "
                f"{DEFAULT_MODEL}{' (dry-run; no quota spent)' if args.dry_run else ''} "
                "— Ctrl-C to stop"
            )
        )
        if not args.dry_run:
            print(
                yellowed(
                    "Warm-up is opt-in and sends real requests: each cold account "
                    "uses five-hour and weekly quota."
                )
            )
        sys.exit(engine.run_loop())
    except ClaudeSwitchError as exc:
        error(f"Error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Five-hour warm-up stopped')}")
        sys.exit(130)


def _config_command(argv: list[str]) -> None:
    """Handle `aswap config [list|get|set|unset|path|export|import]`.

    Pre-dispatched before the main parser is built, like `run` and `auto`
    (same limitation: `config` must be the first argument). Edits the
    settings document (settings.json, stored in the backup root's state.db)
    with strict validation — unlike loading, which forgivingly clamps — so a
    typo'd key or out-of-range value errors loudly here instead of silently
    degrading at `aswap auto` time. `export`/`import` stand in for opening
    the file in an editor, which no longer exists on disk.
    """
    from claude_swap.settings import (
        SETTING_SPECS,
        effective_settings,
        export_settings,
        format_setting_value,
        import_settings,
        set_setting,
        setting_spec,
        settings_path,
        unset_setting,
    )

    key_lines = "\n".join(
        f"  {spec.dotted:<34}{spec.help} (default {format_setting_value(spec.default)})"
        for spec in SETTING_SPECS.values()
    )
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} config",
        description=(
            "Read and edit aswap settings (the settings.json document in "
            "the backup root's state.db)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Keys:
{key_lines}

Examples:
  aswap config                              # list effective settings
  aswap config get autoswitch.threshold
  aswap config set autoswitch.threshold 80
  aswap config unset autoswitch.threshold   # back to the default
  aswap config path                         # where the settings are stored
  aswap config export settings.json         # write them out to hand-edit
  aswap config import settings.json         # validate and load them back

Changes apply to a running `aswap auto` or `aswap codex auto` within one
tick (default 60s), no restart. A key the loop was started with as a flag
(--threshold, --interval, --cooldown, --model, --strategy,
--include-api-key-accounts) keeps the flag's value until that process
restarts. A stored value that fails validation is ignored by a running
loop, which keeps its last good value and logs a warning.
        """,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (with list or get)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(
        dest="action", metavar="{list,get,set,unset,path,export,import}"
    )

    p_list = sub.add_parser("list", help="Show all effective settings (the default)")
    p_get = sub.add_parser("get", help="Print one setting's effective value")
    p_get.add_argument("key", metavar="KEY", help="Dotted key, e.g. autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `aswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON to stdout",
        )
    p_set = sub.add_parser("set", help="Validate and persist one setting")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser("unset", help="Remove one setting (revert to the default)")
    p_unset.add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print where the settings are stored")
    p_export = sub.add_parser(
        "export", help="Write the settings as JSON to FILE (default: stdout)"
    )
    p_export.add_argument("file", metavar="FILE", nargs="?", default="-")
    p_import = sub.add_parser(
        "import",
        help="Replace the settings with FILE ('-' for stdin), validated strictly",
    )
    p_import.add_argument("file", metavar="FILE")

    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action or "list"
    if json_mode and action not in ("list", "get"):
        parser.error("--json can only be used with list or get")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        root = switcher.backup_dir

        if action == "path":
            hit = statedb.locate(settings_path(root))
            if hit is None:  # a symlinked settings.json stays a plain file
                print(settings_path(root))
            else:
                print(f"settings live in {hit[0].path} (document {hit[1]})")
        elif action == "export":
            text = json.dumps(export_settings(root), indent=2) + "\n"
            if args.file == "-":
                sys.stdout.write(text)
            else:
                dest = Path(args.file).expanduser()
                try:
                    dest.write_text(text, encoding="utf-8")
                except OSError as e:
                    raise ConfigError(f"could not write {dest}: {e}") from e
                print(f"Settings exported to {dest}")
        elif action == "import":
            source = "stdin" if args.file == "-" else str(Path(args.file).expanduser())
            try:
                text = (
                    sys.stdin.read() if args.file == "-"
                    else Path(source).read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeDecodeError) as e:
                raise ConfigError(f"could not read {source}: {e}") from e
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{source} is not valid JSON ({e})") from e
            stored = import_settings(root, raw)
            count = sum(len(v) for v in stored.values() if isinstance(v, dict))
            print(f"Imported {count} settings from {source}")
        elif action == "list":
            rows = effective_settings(root)
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "path": str(settings_path(root)),
                    "settings": [
                        {"key": spec.dotted, "value": value, "isSet": is_set}
                        for spec, value, is_set in rows
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                key_w = max(len(spec.dotted) for spec, _, _ in rows)
                val_w = max(len(format_setting_value(v)) for _, v, _ in rows)
                for spec, value, is_set in rows:
                    line = f"{spec.dotted:<{key_w}}  {format_setting_value(value):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            spec = setting_spec(args.key)
            value, is_set = next(
                (v, s) for sp, v, s in effective_settings(root) if sp is spec
            )
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "key": spec.dotted,
                    "value": value,
                    "isSet": is_set,
                }
                print(json.dumps(payload, indent=2))
            else:
                print(format_setting_value(value))
        elif action == "set":
            value = set_setting(root, args.key, args.value)
            print(f"{args.key} = {format_setting_value(value)}")
        elif action == "unset":
            if unset_setting(root, args.key):
                default = setting_spec(args.key).default
                print(f"{args.key} unset (default: {format_setting_value(default)})")
            else:
                print(muted(f"{args.key} is not set; nothing to do"), file=sys.stderr)
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _profile_row(switcher: "ClaudeAccountSwitcher", identifier: str) -> dict:
    """One slot's session-profile row, in the camelCase JSON shape.

    Never carries credential material: booleans, counts, a path and one
    expiry timestamp. Shared by ``profile path --json`` and
    ``profile status``, so the two can't describe the same profile
    differently.
    """
    from claude_swap.json_output import iso_from_epoch_ms

    account_num, email, org_uuid = switcher.resolve_account(identifier)
    snapshot = switcher.profile_snapshot(account_num, email, org_uuid)
    return {
        "number": int(account_num),
        "email": email,
        "path": snapshot["path"],
        "exists": snapshot["exists"],
        "hydrated": snapshot["hydrated"],
        "liveSessions": snapshot["live_sessions"],
        "unreadableRecords": snapshot["unreadable_records"],
        "identityDrifted": snapshot["identity_drifted"],
        "staleMarked": snapshot["stale_marked"],
        "tokenExpiresAt": iso_from_epoch_ms(snapshot["expires_at"]),
    }


def _profile_targets(switcher: "ClaudeAccountSwitcher", account, all_: bool) -> list[str]:
    """Resolve a ``<acct>``/``--all`` selection to slot numbers, in list order."""
    if all_:
        data = switcher._get_sequence_data() or {}
        return [str(num) for num in data.get("sequence", [])]
    return [account]


def _profile_status_line(row: dict) -> str:
    """One human ``profile status`` line. Facts only, no secrets."""
    if not row["exists"]:
        state = muted("no profile")
    elif row["hydrated"]:
        state = accent("hydrated")
    else:
        state = muted("dry")
    notes = []
    if row["liveSessions"]:
        notes.append(f"{row['liveSessions']} live")
    if row["unreadableRecords"]:
        notes.append(f"{row['unreadableRecords']} unreadable")
    if row["identityDrifted"]:
        notes.append("identity drifted")
    if row["staleMarked"]:
        notes.append("stale-marked")
    if row["tokenExpiresAt"]:
        notes.append(f"expires {row['tokenExpiresAt']}")
    suffix = f"  {dimmed('· ' + ', '.join(notes))}" if notes else ""
    return f"  {row['number']}: {row['email']}  {state}{suffix}"


def _profile_sync(switcher: "ClaudeAccountSwitcher", targets: list[str]) -> list[dict]:
    """Run the profile → store adoption pass over the given slots."""
    from claude_swap.switcher import PROFILE_SYNC_ADOPTED, PROFILE_SYNC_FAILED

    rows = []
    for target in targets:
        account_num, email, org_uuid = switcher.resolve_account(target)
        result = switcher.sync_profile_credential(account_num, email, org_uuid)
        rows.append(
            {
                "number": int(account_num),
                "email": email,
                "result": result,
                "adopted": result == PROFILE_SYNC_ADOPTED,
                "failed": result == PROFILE_SYNC_FAILED,
            }
        )
    return rows


@contextlib.contextmanager
def _json_stdout_guard(json_mode: bool):
    """In ``--json`` mode, keep stdout exactly one JSON object.

    The subsystems under this command print human notices with
    ``printer.warning``, which writes to STDOUT — a ``setup_session`` whose
    pre-refresh failed emits one, and it lands in the middle of the
    payload and breaks every parser. Route those to stderr for the
    duration rather than suppressing them: the notice is still worth
    reading, just not by the thing parsing the result.
    """
    if not json_mode:
        yield
        return
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _profile_hydrate(switcher: "ClaudeAccountSwitcher", targets: list[str]) -> list[dict]:
    """Ensure each slot's profile holds usable credential material.

    A thin shell over ``setup_session``'s existing housekeeping mode — the
    same call ``warmup`` uses — so every refusal it already raises (API-key
    slots, a spent-and-unstashed grant, an identity that moved mid-prepare)
    is inherited rather than reimplemented. Sharing changes are off: a
    hydrate must not rewrite MCP mirrors or history links behind the user's
    back.

    A profile with a live (or unreadably-occupied) claude in it is SKIPPED,
    not prepared. Two reasons, and either would be enough. It is already
    hydrated — a running claude is proof of a working login in there, so
    there is nothing to do. And `setup_session` would try: its pre-bootstrap
    refresh POSTs a one-time-use grant that process still needs, and its
    final `_bootstrap` deletes the profile's Keychain entry and overwrites
    its credential file. Both are survivable once by hand; across a
    `--all` sweep of a resident fleet they are not.

    One bad slot never aborts the sweep; failures are collected and
    reported per slot.
    """
    from claude_swap.exceptions import ClaudeSwitchError
    from claude_swap.session import SessionManager

    manager = SessionManager(switcher)
    rows: list[dict] = []
    for target in targets:
        try:
            account_num, email, org_uuid = switcher.resolve_account(target)
        except ClaudeSwitchError as exc:
            rows.append({"number": None, "email": target, "hydrated": False,
                         "error": str(exc)})
            continue
        skip = switcher.hydration_skip_reason(account_num)
        if skip is None:
            snapshot = switcher.profile_snapshot(account_num, email, org_uuid)
            if snapshot["live_sessions"] or snapshot["unreadable_records"]:
                skip = "profile-in-use"
        if skip is not None:
            rows.append({"number": int(account_num), "email": email,
                         "hydrated": False, "skipped": skip})
            continue
        try:
            manager.setup_session(
                account_num,
                share=False,
                share_history=False,
                sync_sharing=False,
                refresh_credentials=True,
                expected_identity=(email, org_uuid),
            )
        except ClaudeSwitchError as exc:
            rows.append({"number": int(account_num), "email": email,
                         "hydrated": False, "error": str(exc)})
            continue
        rows.append({"number": int(account_num), "email": email, "hydrated": True})
    return rows


def _profile_dehydrate(
    switcher: "ClaudeAccountSwitcher", targets: list[str], force: bool
) -> list[dict]:
    """Drop each slot's profile credential material, keeping its history.

    Refuses a profile that is not provably quiescent — a live claude, or a
    session record we could not read, which is not the same as nobody being
    there. ``--force`` overrides, loudly: the running process keeps its own
    in-memory copy but will re-bootstrap on its next launch.
    """
    rows: list[dict] = []
    for target in targets:
        account_num, email, _org = switcher.resolve_account(target)
        snapshot = switcher.profile_snapshot(account_num, email)
        busy = snapshot["live_sessions"] or snapshot["unreadable_records"]
        if busy and not force:
            rows.append({
                "number": int(account_num), "email": email, "dehydrated": False,
                "skipped": "profile-in-use",
                "liveSessions": snapshot["live_sessions"],
                "unreadableRecords": snapshot["unreadable_records"],
            })
            continue
        if busy:
            warning(
                f"Account-{account_num} ({email}) has a live or unreadable "
                "session profile; dehydrating it anyway (--force). The "
                "running instance keeps working until it is restarted."
            )
        switcher._invalidate_session_credentials(account_num, email)
        rows.append({"number": int(account_num), "email": email,
                     "dehydrated": True, "forced": bool(busy)})
    return rows


def _profile_command(argv: list[str]) -> None:
    """Handle `aswap profile [path|status|sync|hydrate|dehydrate] ...`.

    Pre-dispatched before the main parser is built, like `config` (same
    limitation: `profile` must be the first argument). **No subcommand ever
    prints, logs or returns credential material** — only booleans, counts,
    paths and an expiry timestamp.

    `sync` writes only the account store, and only bytes the profile
    already holds: it POSTs nothing, consumes no grant and spends no quota.
    `hydrate` and `dehydrate` write the profile itself.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} profile",
        description=(
            "Inspect the per-account session profiles `aswap run` uses "
            "(and that an external launcher can point CLAUDE_CONFIG_DIR at)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  aswap profile path 2                # print the profile dir, nothing else
  aswap profile status 2
  aswap profile status --all --json
  aswap profile sync --all            # adopt rotated profile tokens (no POST)
  aswap profile hydrate --all         # make every eligible profile logged in
  aswap profile dehydrate 2           # drop its credentials, keep its history
        """,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(
        dest="action", metavar="{path,status,sync,hydrate,dehydrate}"
    )

    p_path = sub.add_parser("path", help="Print one account's profile directory")
    p_path.add_argument("account", metavar="NUM|EMAIL|ALIAS")

    p_status = sub.add_parser("status", help="Report profile state (no secrets)")
    p_sync = sub.add_parser(
        "sync",
        help="Adopt a profile's rotated credential into the store",
        description=(
            "Copy a session profile's newer credential into the account "
            "store. Nothing is sent anywhere and no grant is consumed; a "
            "wiped, stale, drifted or unreadable profile is refused."
        ),
    )
    p_hydrate = sub.add_parser(
        "hydrate",
        help="Ensure a profile holds usable credentials",
        description=(
            "Prepare a session profile so Claude Code finds a login in it. "
            "Idempotent: an already-valid profile is left alone. Disabled, "
            "API-key and empty slots are skipped, and one failing slot never "
            "aborts a --all sweep."
        ),
    )
    p_dehydrate = sub.add_parser(
        "dehydrate",
        help="Remove a profile's credentials (history is kept)",
        description=(
            "Drop a session profile's credential material so the next launch "
            "re-bootstraps from the store. Refuses a profile that is live, "
            "or whose session records could not be read."
        ),
    )
    p_dehydrate.add_argument(
        "--force",
        action="store_true",
        help="Dehydrate even a live or unreadable profile",
    )
    for p in (p_status, p_sync, p_hydrate, p_dehydrate):
        p.add_argument("account", nargs="?", metavar="NUM|EMAIL|ALIAS")
        p.add_argument(
            "--all", dest="all_", action="store_true", help="Every managed account"
        )

    for p in (p_path, p_status, p_sync, p_hydrate, p_dehydrate):
        p.add_argument(
            "--json", action="store_true", help="Emit machine-readable JSON"
        )

    args = parser.parse_args(argv)
    if args.action is None:
        parser.error(
            "a subcommand is required: path, status, sync, hydrate or dehydrate"
        )
    json_mode = bool(getattr(args, "json", False))
    all_ = bool(getattr(args, "all_", False))
    if args.action in ("status", "sync", "hydrate", "dehydrate"):
        if all_ and args.account is not None:
            parser.error("--all does not take an account argument")
        if not all_ and args.account is None:
            parser.error("give an account, or --all")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.action == "path":
            row = _profile_row(switcher, args.account)
            if json_mode:
                print(json.dumps({"schemaVersion": 1, **row}, indent=2))
            else:
                # Bare path and nothing else, so it composes in a shell.
                print(row["path"])
            return

        targets = _profile_targets(switcher, args.account, all_)

        if args.action == "sync":
            with _json_stdout_guard(json_mode):
                results = _profile_sync(switcher, targets)
            if json_mode:
                print(
                    json.dumps(
                        {"schemaVersion": 1, "results": results}, indent=2
                    )
                )
            else:
                for row in results:
                    label = f"  {row['number']}: {row['email']}"
                    if row["adopted"]:
                        print(f"{label}  {accent('adopted')}")
                    elif row["failed"]:
                        print(f"{label}  {yellowed('write failed')}")
                    else:
                        print(f"{label}  {dimmed(row['result'])}")
            # A slot whose profile simply had nothing newer is not a failure;
            # only a write that could not land is.
            if any(row["failed"] for row in results):
                sys.exit(1)
            return

        if args.action in ("hydrate", "dehydrate"):
            verb = "hydrated" if args.action == "hydrate" else "dehydrated"
            with _json_stdout_guard(json_mode):
                results = (
                    _profile_hydrate(switcher, targets)
                    if args.action == "hydrate"
                    else _profile_dehydrate(switcher, targets, args.force)
                )
            if json_mode:
                print(
                    json.dumps(
                        {"schemaVersion": 1, "results": results}, indent=2
                    )
                )
            else:
                for row in results:
                    label = f"  {row.get('number') or '?'}: {row['email']}"
                    if row[verb]:
                        print(f"{label}  {accent(verb)}")
                    elif row.get("skipped"):
                        print(f"{label}  {dimmed('skipped: ' + row['skipped'])}")
                    else:
                        print(f"{label}  {yellowed(row.get('error', 'failed'))}")
            # Exit 1 only on a real failure: a deliberately skipped slot
            # (disabled, API-key, empty, in use) is the command doing its job.
            if any(
                not row[verb] and not row.get("skipped") for row in results
            ):
                sys.exit(1)
            return

        rows = [
            _profile_row(switcher, target)
            for target in targets
        ]
        if json_mode:
            print(json.dumps({"schemaVersion": 1, "profiles": rows}, indent=2))
        else:
            if not rows:
                print(dimmed("No accounts are managed yet."))
                return
            print(bolded("Session profiles:"))
            for row in rows:
                print(_profile_status_line(row))
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _usage_command(argv: list[str]) -> None:
    """Handle `aswap usage refresh [--max-age S] [--account N] [--json]`.

    Pre-dispatched before the main parser, like `config` and `profile`.
    Steps around the poll cadence for a caller that needs a better number
    than the serve TTL is willing to hand out — with brakes, because that
    cadence is a measured request budget and not a preference. See
    `ClaudeAccountSwitcher.refresh_usage`.
    """
    import math

    from claude_swap.usage_store import MIN_FORCED_AGE_S

    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} usage",
        description="Inspect and refresh the per-account usage cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  aswap usage refresh                 # re-poll anything older than 60s
  aswap usage refresh --max-age 120
  aswap usage refresh --account 2 --json

`refresh` calls the OAuth usage-metadata endpoint -- the same free request
every poll already makes. It never spends paid tokens. It will not fetch a
row fresher than {MIN_FORCED_AGE_S:.0f}s, and one forced sweep per
{MIN_FORCED_AGE_S:.0f}s is allowed across all processes, so a caller in a
loop cannot hammer the endpoint. Dead-token quarantines and 429 backoffs
are still honoured.
        """,
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="action", metavar="{refresh}")

    p_refresh = sub.add_parser("refresh", help="Force a usage poll (free endpoint)")
    p_refresh.add_argument(
        "--max-age",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help=(
            f"Only re-poll accounts whose number is older than this "
            f"(default 60; floored at {MIN_FORCED_AGE_S:.0f})"
        ),
    )
    p_refresh.add_argument(
        "--account", metavar="NUM|EMAIL|ALIAS", help="Limit to one account"
    )
    p_refresh.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )

    args = parser.parse_args(argv)
    if args.action is None:
        parser.error("a subcommand is required: refresh")
    # argparse's `type=float` happily accepts "nan" and "inf", and `nan`
    # defeats the floor: `max(nan, 30.0)` is `nan`, and every `age < nan`
    # comparison is False, so nothing would ever be considered fresh enough
    # to skip. Reject both here rather than let a typo turn the brake off.
    if not math.isfinite(args.max_age) or args.max_age < 0:
        parser.error("--max-age must be a finite, non-negative number of seconds")
    json_mode = bool(getattr(args, "json", False))

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        with _json_stdout_guard(json_mode):
            results = switcher.refresh_usage(
                max_age_s=args.max_age, account=args.account
            )

        if json_mode:
            print(json.dumps({"schemaVersion": 1, "results": results}, indent=2))
            return
        if not results:
            print(dimmed("No accounts are managed yet."))
            return
        for row in results:
            label = f"  {row['number']}: {row['email']}"
            if row["fetched"]:
                print(f"{label}  {accent('refreshed')}")
            else:
                print(f"{label}  {dimmed(row['skippedReason'] or 'unchanged')}")
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    try:
        endpoints.validate_environment()
    except endpoints.EndpointOverrideError as e:
        # Before anything can send a token: a refused override never falls
        # back to some other host.
        error(f"Error: {e}")
        sys.exit(2)
    argv = sys.argv[1:]
    try:
        from claude_swap.appearance import cli_should_probe, cli_theme
        # `run` execs a child that takes over the terminal, and `--json`
        # must stay machine-readable — never probe (and emit the OSC query)
        # in either case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it

    # `run`, `auto`, and `warmup` keep their dedicated pre-dispatch parsers.
    if argv and argv[0] == "run":
        _run_command(argv[1:])
        return  # only reachable in tests where exec/exit is mocked
    if argv and argv[0] == "codex":
        _codex_command(argv[1:])
        return
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if argv and argv[0] == "warmup":
        _warmup_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_command(sys.argv[2:])
        return
    if argv and argv[0] == "profile":
        _profile_command(argv[1:])
        return
    if argv and argv[0] == "usage":
        _usage_command(argv[1:])
        return
    if argv and argv[0] == "backup":
        from claude_swap.snapshots import backup_command

        backup_command(argv[1:])
        return
    if argv and argv[0] == "billing":
        from claude_swap.billing_cli import billing_command

        billing_command(argv[1:])
        return
    if argv and argv[0] == "map":
        _map_command(argv[1:])
        return
    if argv and argv[0] == "unmap":
        _unmap_command(argv[1:])
        return
    if argv and argv[0] == "unclaimed":
        _unclaimed_command(argv[1:])
        return
    if argv and argv[0] == "alias":
        _alias_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return
    if argv and argv[0] == "renumber":
        _renumber_command(argv[1:])
        return
    if argv and argv[0] == "priority":
        _priority_command(argv[1:])
        return

    # Bare `aswap` in an interactive terminal opens the TUI dashboard (like
    # lazygit/k9s). TTY-gated on both ends so scripts and pipes keep getting
    # the usage error, and `aswap tui` stays the explicit spelling.
    if not argv and sys.stdout.isatty() and sys.stdin.isatty():
        argv = ["--tui"]

    # Memorable subcommands (`aswap switch <email>`, `aswap list`, `aswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        usage="%(prog)s <command> [args] [options]",
        description="""Multi-Account Switcher for Claude Code and Codex

Commands:
  %(prog)s help                       show this help
  %(prog)s list                       list managed accounts
  %(prog)s status                     show current account
  %(prog)s switch                     rotate to the next account
  %(prog)s switch <num|email>         switch to a specific account
  %(prog)s add                        add the current account
  %(prog)s add-token [TOKEN|-]        register a setup-token or API key
  %(prog)s remove <num|email>         remove an account
  %(prog)s disable <num|email>        hold an account out of auto-rotation
  %(prog)s enable <num|email>         return a disabled account to rotation
  %(prog)s run <num|email> [-- ...]   run as an account, this terminal only
  %(prog)s run                        run the current dir's mapped account
  %(prog)s map <num|email> [path]     map a directory to an account
  %(prog)s map                        list directory mappings
  %(prog)s unmap [path]               remove a directory mapping
  %(prog)s alias <num|email> <name>   set a short alias for an account
  %(prog)s alias <num|email> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s renumber [--by-email]      renumber every account to 1..N
  %(prog)s priority [<num|email>...]  accounts auto uses up first, in order
  %(prog)s codex <command>            manage Codex CLI accounts
  %(prog)s backup [take|list|check|restore|export]  snapshots of state.db; crash restore
  %(prog)s billing <command>          exact billing, weekly resets, resub plan
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s warmup                     keep five-hour windows ready (opt-in spend)
  %(prog)s config [set KEY VALUE]     show or change settings (settings.json)
  %(prog)s profile <command>          inspect/reconcile session profiles
  %(prog)s usage refresh              force a usage poll (free endpoint)
  %(prog)s unclaimed [--purge ID]     list or drop stashed credential entries
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s tui                        interactive dashboard (also: bare %(prog)s)
  %(prog)s watch                      dashboard, opened on the live watch page
  %(prog)s menubar                    macOS menu bar app
  %(prog)s upgrade                    self-upgrade to latest
  %(prog)s purge                      remove all aswap data

Aliases: ls=list  rm=remove  update=upgrade""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s switch --strategy next-available # rotate, skipping rate-limited accounts
  %(prog)s switch user@example.com
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s add --slot 3                      # add to a specific slot
  %(prog)s add-token sk-ant-oat01-... --email me@example.com
  %(prog)s run 2 -- --resume                 # forward args after '--' to claude
  %(prog)s auto --once                       # single auto-switch tick (cron-friendly)
  %(prog)s warmup --once --dry-run            # inspect warm-up decisions, spend nothing
  %(prog)s config set autoswitch.threshold 80
  %(prog)s codex switch 2                    # switch the file-backed Codex login

The original flag spellings (%(prog)s --switch, %(prog)s --list, ...) keep working.
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show source-labelled OAuth token diagnostics (use with 'list')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with 'list', 'status', "
            "or 'switch'). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "codex", "all"],
        default="all",
        metavar="{claude,codex,all}",
        help=(
            "Which provider 'list' covers. 'all' (default) merges Claude Code "
            "and Codex; 'claude' or 'codex' restricts to one. In --json mode a "
            "single provider emits that provider's bare payload; 'all' nests "
            "both under 'claude'/'codex' keys."
        ),
    )
    parser.add_argument(
        "--profiles",
        action="store_true",
        help=(
            "With 'list --json': add each account's session-profile state "
            "(hydrated, live sessions, identity drift, token expiry). Costs "
            "one small read and a PID scan per account, so it is opt-in."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With bare 'switch': pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With 'switch --strategy': also count these models' per-model "
            "weekly limits when comparing accounts (comma-separated display "
            "names, or 'all'). Defaults to the autoswitch.model setting"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with 'add' or 'add-token')",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with 'add-token'; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with 'export')",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        help="Set a short display alias for the account (use with 'add')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with 'switch <num|email>', "
            "activate the stored credentials without backing up the current "
            "login first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )

    # Legacy `--flag` interface. Still fully supported (bare subcommands rewrite
    # into these, see _translate_subcommand), but hidden from --help so the
    # subcommands shown in the description are the one documented interface.
    # The group is not `required` because the "no command" case is handled
    # explicitly below (a required group with every member suppressed prints a
    # broken empty-list error).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--add-account",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--disable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--enable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--status",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--tui",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--watch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--menubar",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    # No action selected: emit a clean, subcommand-oriented message rather than
    # the raw argparse "one of the arguments ... is required" (which would list
    # the now-hidden legacy flags). Value actions can be falsy-but-set
    # (--add-token uses const=""), so test those with `is not None`.
    if not (
        args.add_account
        or args.list
        or args.switch
        or args.status
        or args.purge
        or args.tui
        or args.watch
        or args.menubar
        or args.upgrade
        or args.remove_account is not None
        or args.disable_account is not None
        or args.enable_account is not None
        or args.switch_to is not None
        or args.export is not None
        or args.import_ is not None
        or args.add_token is not None
    ):
        parser.error("no command given — try '%(prog)s help'" % {"prog": _prog_name()})

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with 'list'")

    if args.provider != "all" and not args.list:
        parser.error("--provider can only be used with 'list'")

    if args.token_status and args.provider == "codex":
        parser.error("--token-status applies to Claude Code, not '--provider codex'")

    if args.profiles and not (args.list and args.json):
        # The profile block has no human rendering — `aswap profile status`
        # is that surface — so silently ignoring it here would be a lie.
        parser.error("--profiles can only be used with 'list --json'")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with 'list', 'status', or 'switch'")

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with bare 'switch'")

    if args.model is not None and args.strategy is None:
        # Meaningless on a direct-target switch or plain rotation — nothing
        # usage-aware reads it there, so reject loudly rather than ignore.
        parser.error(
            "--model can only be used with 'switch --strategy best' or "
            "'switch --strategy next-available'"
        )

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with 'add' or 'add-token'")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with 'add-token'")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with 'export'")

    if args.alias is not None and not args.add_account:
        parser.error("--alias can only be used with 'add'")

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with 'import' or 'switch <num|email>'")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from claude_swap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            switcher.add_account(slot=args.slot, alias=args.alias)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = _list_dispatch(switcher, args)
        elif args.switch:
            from claude_swap.settings import load_settings, parse_model_names

            # Only the usage-aware strategies read model limits: --model wins;
            # otherwise the persistent autoswitch.model setting applies
            # (announced by switch(), never silently).
            if args.strategy is None:
                models, model_source = (), None
            elif args.model is not None:
                models, model_source = parse_model_names(args.model), "cli"
            else:
                models = parse_model_names(load_settings(switcher.backup_dir).model)
                model_source = "autoswitch.model" if models else None
            payload = switcher.switch(
                strategy=args.strategy,
                json_output=args.json,
                models=models,
                model_source=model_source,
            )
            if payload is not None and models:
                payload["models"] = list(models)
                payload["modelSource"] = model_source
        elif args.switch_to:
            payload = switcher.switch_to(
                args.switch_to, json_output=args.json, force=args.force
            )
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
        elif args.export:
            from claude_swap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from claude_swap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.tui:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher))
        elif args.watch:
            from claude_swap.tui import run as tui_run

            sys.exit(tui_run(switcher, start="watch"))
        elif args.menubar:
            if sys.platform != "darwin":
                error("The menu bar is only available on macOS.")
                sys.exit(1)
            # menubar is import-safe without the extra; a missing rumps
            # surfaces from run() as a ClaudeSwitchError with the install hint.
            from claude_swap.menubar import run as menubar_run


            sys.exit(menubar_run(switcher))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # No passive update notification: aswap is not on PyPI, and the PyPI
    # name "aswap" belongs to whoever registers it, so asking PyPI for a
    # newer version would advertise someone else's package.


if __name__ == "__main__":
    main()
