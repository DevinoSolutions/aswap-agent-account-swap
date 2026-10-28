"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    atomic_write_json,
    AutoSwitchSettings,
    ProfileSettings,
    UiSettings,
    effective_settings,
    load_profile_settings,
    load_settings,
    load_ui_settings,
    cli_overrides,
    merged_with_cli,
    reload_autoswitch_settings,
    save_settings,
    set_setting,
    settings_fingerprint,
    settings_path,
    unset_setting,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
        "strategy": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestLoadSettings:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_corrupt_file_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_non_object_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("[1, 2]")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 80.0
        assert loaded.interval_seconds == AutoSwitchSettings().interval_seconds
        assert loaded.warmup_five_hour is False

    def test_loads_five_hour_warmup_opt_in(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"warmupFiveHour": True}})
        )

        assert load_settings(tmp_path).warmup_five_hour is True

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
    def test_five_hour_warmup_requires_a_json_boolean(self, tmp_path: Path, value):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"warmupFiveHour": value}})
        )

        assert load_settings(tmp_path).warmup_five_hour is False

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
    def test_autoswitch_enabled_requires_a_json_boolean(
        self, tmp_path: Path, value
    ):
        """Falls back to the DEFAULT (true) — never to a guess."""
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"enabled": value}})
        )

        assert load_settings(tmp_path).enabled is True

    def test_auto_add_accounts_is_on_by_default(self, tmp_path: Path):
        assert load_settings(tmp_path).auto_add_accounts is True

    def test_auto_add_accounts_can_be_turned_off(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"autoAddAccounts": False}})
        )

        assert load_settings(tmp_path).auto_add_accounts is False

    @pytest.mark.parametrize("value", ["false", 0, None])
    def test_auto_add_accounts_requires_a_json_boolean(self, tmp_path: Path, value):
        """A hand-edited "false" must not read as true — nor silently as off."""
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"autoAddAccounts": value}})
        )

        assert load_settings(tmp_path).auto_add_accounts is True

    @pytest.mark.parametrize(
        "key,section,loader",
        [
            ("enabled", "autoswitch", lambda p: load_settings(p)),
            ("warmupFiveHour", "autoswitch", lambda p: load_settings(p)),
            ("autoAddAccounts", "autoswitch", lambda p: load_settings(p)),
            ("persistent", "profiles", lambda p: load_profile_settings(p)),
        ],
    )
    def test_a_reverted_strict_bool_says_so(
        self, tmp_path: Path, key, section, loader, caplog
    ):
        """Silence is how a hand-edited "false" keeps aswap doing the thing
        the user believed they had just switched off."""
        settings_path(tmp_path).write_text(
            json.dumps({section: {key: "false"}})
        )

        with caplog.at_level("WARNING", logger="claude-swap"):
            loader(tmp_path)

        assert any(
            f"{section}.{key}" in r.getMessage() and "true or false" in r.getMessage()
            for r in caplog.records
        )

    def test_values_are_clamped(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "threshold": 200,
                "intervalSeconds": 1,
                "hysteresisPct": -5,
                "unhealthyTicks": 0,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 99.9
        assert loaded.interval_seconds == 15.0  # usage-cache TTL floor
        assert loaded.hysteresis_pct == 0.0
        assert loaded.unhealthy_ticks == 1

    def test_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {"threshold": "high", "includeApiKeyAccounts": 1}
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == AutoSwitchSettings().threshold
        assert loaded.include_api_key_accounts is True

    def test_unsupported_strategy_falls_back_to_best(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "chaos"}})
        )
        assert load_settings(tmp_path).strategy == "best"

    def test_consume_first_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "consume-first"}})
        )
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_set_strategy_consume_first(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "consume-first")
        assert load_settings(tmp_path).strategy == "consume-first"


class TestSaveSettings:
    def test_roundtrip(self, tmp_path: Path):
        custom = AutoSwitchSettings(threshold=85.0, cooldown_seconds=60.0)
        save_settings(tmp_path, custom)
        assert load_settings(tmp_path) == custom

    def test_unknown_keys_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        mode = stat.S_IMODE(settings_path(tmp_path).stat().st_mode)
        assert mode == 0o600


class TestUiSettings:
    def test_missing_file_defaults_to_auto(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto")

    def test_reads_auto(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "auto"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_reads_light(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_ui_settings(tmp_path).theme == "light"

    def test_unknown_theme_clamps_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "purple"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_set_and_unset_ui_theme(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.theme", "light") == "light"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "ui": {"theme": "light"}}
        assert unset_setting(tmp_path, "ui.theme") is True
        assert "ui" not in json.loads(settings_path(tmp_path).read_text())

    def test_ui_view_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.view", "codex") == "codex"
        assert load_ui_settings(tmp_path).view == "codex"

    def test_invalid_theme_does_not_reset_view(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"ui": {"theme": "chartreuse", "view": "codex"}})
        )
        loaded = load_ui_settings(tmp_path)
        assert loaded.theme == "auto"
        assert loaded.view == "codex"

    def test_set_rejects_bad_choice(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="dark, light"):
            set_setting(tmp_path, "ui.theme", "purple")


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        by_section: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_section.setdefault(spec.section, set()).add(spec.field)
        assert by_section["autoswitch"] == {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert by_section["ui"] == {
            f.name for f in UiSettings.__dataclass_fields__.values()
        }
        assert by_section["profiles"] == {
            f.name for f in ProfileSettings.__dataclass_fields__.values()
        }

    def test_defaults_match_dataclass(self):
        sources = {
            "autoswitch": AutoSwitchSettings(),
            "profiles": ProfileSettings(),
            "ui": UiSettings(),
        }
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(sources[spec.section], spec.field)


class TestSetUnsetSetting:
    def test_set_writes_minimal_file(self, tmp_path: Path):
        value = set_setting(tmp_path, "autoswitch.threshold", "80")
        assert value == 80.0
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}

    def test_set_int_kind_coerces_and_rejects_floats(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.unhealthyTicks", "5") == 5
        with pytest.raises(ConfigError, match="integer"):
            set_setting(tmp_path, "autoswitch.unhealthyTicks", "3.5")

    def test_set_rejects_out_of_range_without_writing(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "200")
        assert not settings_path(tmp_path).exists()

    def test_set_rejects_unknown_key(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.bogus", "1")

    def test_set_string_kind_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.model", "Fable") == "Fable"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["model"] == "Fable"
        assert load_settings(tmp_path).model == "Fable"

    def test_set_string_kind_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unset"):
            set_setting(tmp_path, "autoswitch.model", "   ")
        assert not settings_path(tmp_path).exists()

    def test_garbage_model_value_falls_back_to_none(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"model": 123}})
        )
        assert load_settings(tmp_path).model is None

    def test_set_rejects_bool_words_strictly(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "FALSE") is False
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "falsy")

    def test_set_on_corrupt_file_raises_and_preserves_it(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert settings_path(tmp_path).read_text() == "{not json"

    def test_unset_removes_key_and_empty_section(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert "autoswitch" not in raw

    def test_unset_stamps_schema_version_on_unversioned_file(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"threshold": 80}})
        )
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 1

    def test_unset_absent_key_is_noop(self, tmp_path: Path):
        assert unset_setting(tmp_path, "autoswitch.threshold") is False
        assert not settings_path(tmp_path).exists()


class TestEffectiveSettings:
    def test_missing_file_reports_all_defaults(self, tmp_path: Path):
        rows = effective_settings(tmp_path)
        assert len(rows) == len(SETTING_SPECS)
        assert all(not is_set for _, _, is_set in rows)

    def test_presence_not_value_equality_marks_set(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "90")  # equals default
        by_key = {spec.dotted: is_set for spec, _, is_set in effective_settings(tmp_path)}
        assert by_key["autoswitch.threshold"] is True
        assert by_key["autoswitch.intervalSeconds"] is False


class TestMergedWithCli:
    def test_no_flags_returns_settings_unchanged(self):
        base = AutoSwitchSettings(threshold=80.0)
        assert merged_with_cli(base, _args()) is base

    def test_cli_beats_settings(self):
        base = AutoSwitchSettings(threshold=80.0, cooldown_seconds=10.0)
        merged = merged_with_cli(base, _args(threshold=60.0, interval=30.0))
        assert merged.threshold == 60.0
        assert merged.interval_seconds == 30.0
        assert merged.cooldown_seconds == 10.0  # untouched

    def test_cli_values_are_clamped(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(interval=1.0))
        assert merged.interval_seconds == 15.0

    def test_boolean_override(self):
        merged = merged_with_cli(
            AutoSwitchSettings(), _args(include_api_key_accounts=True)
        )
        assert merged.include_api_key_accounts is True

    def test_model_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(model="Fable"))
        assert merged.model == "Fable"

    def test_strategy_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(strategy="consume-first"))
        assert merged.strategy == "consume-first"


class TestReloadAutoswitchSettings:
    """The strict re-read a running ``aswap auto`` applies mid-run."""

    def _write(self, root: Path, section) -> None:
        settings_path(root).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": section})
        )

    def test_valid_values_are_taken(self, tmp_path: Path):
        self._write(tmp_path, {"threshold": 72, "autoAddAccounts": False})
        loaded, problems = reload_autoswitch_settings(tmp_path, AutoSwitchSettings())
        assert problems == []
        assert loaded.threshold == 72.0
        assert loaded.auto_add_accounts is False

    def test_an_absent_key_is_its_default_not_the_previous_value(self, tmp_path: Path):
        # `aswap config unset` removes the key: that asks for the default.
        self._write(tmp_path, {})
        previous = AutoSwitchSettings(threshold=72.0, strategy="consume-first")
        loaded, problems = reload_autoswitch_settings(tmp_path, previous)
        assert problems == []
        assert loaded == AutoSwitchSettings()

    def test_missing_document_is_all_defaults(self, tmp_path: Path):
        loaded, problems = reload_autoswitch_settings(
            tmp_path, AutoSwitchSettings(threshold=72.0)
        )
        assert (loaded, problems) == (AutoSwitchSettings(), [])

    @pytest.mark.parametrize("key, bad", [
        ("threshold", 120),        # out of range: load would clamp to 99.9
        ("threshold", "80"),       # a quoted number
        ("autoAddAccounts", "false"),  # truthy string
        ("strategy", "fastest"),   # not a choice
        ("unhealthyTicks", 2.5),   # not an integer
        ("intervalSeconds", True),
    ])
    def test_an_invalid_value_keeps_the_previous_one(self, tmp_path: Path, key, bad):
        spec = SETTING_SPECS[f"autoswitch.{key}"]
        previous = AutoSwitchSettings(
            threshold=72.0, auto_add_accounts=False, strategy="consume-first",
            unhealthy_ticks=7, interval_seconds=120.0,
        )
        self._write(tmp_path, {key: bad, "cooldownSeconds": 5})
        loaded, problems = reload_autoswitch_settings(tmp_path, previous)
        assert getattr(loaded, spec.field) == getattr(previous, spec.field)
        assert loaded.cooldown_seconds == 5.0  # the valid key still applies
        assert len(problems) == 1
        assert problems[0].startswith(f"autoswitch.{key} ")
        assert "keeping" in problems[0]

    def test_a_null_model_is_the_default(self, tmp_path: Path):
        # save_settings writes every key, an unset model as null.
        self._write(tmp_path, {"model": None})
        loaded, problems = reload_autoswitch_settings(
            tmp_path, AutoSwitchSettings(model="Fable")
        )
        assert loaded.model is None
        assert problems == []

    def test_an_unparseable_document_keeps_everything(self, tmp_path: Path):
        previous = AutoSwitchSettings(threshold=72.0)
        settings_path(tmp_path).write_text("{not json")
        loaded, problems = reload_autoswitch_settings(tmp_path, previous)
        assert loaded is previous
        assert len(problems) == 1 and "keeping the current settings" in problems[0]

    def test_a_non_object_section_keeps_everything(self, tmp_path: Path):
        previous = AutoSwitchSettings(threshold=72.0)
        self._write(tmp_path, [1, 2])
        loaded, problems = reload_autoswitch_settings(tmp_path, previous)
        assert loaded is previous
        assert problems


class TestSettingsFingerprint:
    def test_absent_is_none(self, tmp_path: Path):
        assert settings_fingerprint(tmp_path) is None

    def test_follows_the_document_write_time(self, tmp_path: Path):
        import os

        set_setting(tmp_path, "autoswitch.threshold", "80")
        first = settings_fingerprint(tmp_path)
        assert first is not None
        os.utime(settings_path(tmp_path), (first + 5, first + 5))
        assert settings_fingerprint(tmp_path) == first + 5


class TestCliOverrides:
    def test_only_given_flags(self):
        assert cli_overrides(_args()) == {}
        assert cli_overrides(_args(threshold=70.0, include_api_key_accounts=False)) == {
            "threshold": 70.0, "include_api_key_accounts": False,
        }


class TestAtomicWriteThroughSymlink:
    """A rename does not follow links, so renaming onto a symlinked path
    detaches it and the target silently stops updating. Covers the write
    itself plus the two placement decisions it forces: the temp file goes
    beside the RESOLVED target (else EXDEV across mounts), the 0700 chmod
    stays on the directory aswap owns (else it narrows — or cannot touch —
    a foreign one)."""

    def test_write_preserves_the_link_and_updates_the_target(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"
        tracked.write_text(json.dumps({"tracked": True}))
        link = live / "settings.json"
        link.symlink_to(tracked)

        atomic_write_json(link, {"written": "through"})

        assert link.is_symlink(), "the dotfiles link must survive the write"
        assert json.loads(tracked.read_text()) == {"written": "through"}

    def test_dangling_link_writes_where_it_points(self, tmp_path):
        target = tmp_path / "gone" / "settings.json"
        link = tmp_path / "settings.json"
        link.symlink_to(target)

        atomic_write_json(link, {"dangling": "ok"})

        assert link.is_symlink()
        assert json.loads(target.read_text()) == {"dangling": "ok"}

    def test_plain_file_write_unchanged(self, tmp_path):
        p = tmp_path / "settings.json"
        atomic_write_json(p, {"plain": 1})
        assert not p.is_symlink()
        assert json.loads(p.read_text()) == {"plain": 1}

    def test_temp_file_is_created_beside_the_target(self, tmp_path, monkeypatch):
        """Beside the LINK, the rename hits EXDEV whenever the target is on
        another mount — the write fails outright. Assert the placement
        directly; staging two filesystems in a unit test is not portable."""
        import tempfile
        from claude_swap import settings as S
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)
        seen = []
        real_mkstemp = tempfile.mkstemp
        monkeypatch.setattr(
            S.tempfile, "mkstemp",
            lambda *a, **kw: (seen.append(kw.get("dir")), real_mkstemp(*a, **kw))[1],
        )

        atomic_write_json(link, {"x": 1})

        assert seen == [str(repo)], f"tmp must land beside the target, got {seen}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_hardening_stays_on_the_directory_aswap_owns(self, tmp_path):
        """The 0700 belongs to aswap's own dir. On the target's parent it
        would narrow a foreign directory, and raise PermissionError when
        that parent cannot be chmod'ed at all."""
        repo = tmp_path / "repo"; repo.mkdir(mode=0o755)
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)

        atomic_write_json(link, {"x": 1})

        assert (repo.stat().st_mode & 0o777) == 0o755, "foreign dir untouched"
        assert (live.stat().st_mode & 0o777) == 0o700, "our dir hardened"
        assert (tracked.stat().st_mode & 0o777) == 0o600, "file still 0600"
