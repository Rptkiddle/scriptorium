"""Server settings: defaults, the file, validation, masking of secrets."""

import stat

import pytest

from scriptorium_server import settings
from scriptorium_server.settings import SETTINGS, SettingsError, load, set_value, show


@pytest.fixture
def path(tmp_path):
    return tmp_path / "etc" / "settings.toml"


def test_without_a_file_every_setting_has_its_default(path):
    current = load(path)
    assert all(current[s.name] == s.default for s in SETTINGS)
    assert current["limits.per_request"] == 1000 and not path.exists()


def test_a_set_value_is_read_back_and_the_rest_stay_default(path):
    set_value("limits.per_request", "250", path)  # as typed at the command line
    set_value("surveillance.day", "monday", path)
    current = load(path)
    assert current["limits.per_request"] == 250 and current["surveillance.day"] == "monday"
    assert current["backup.days_kept"] == 14
    assert path.read_text() == '[surveillance]\nday = "monday"\n\n[limits]\nper_request = 250\n'


def test_setting_the_default_removes_the_entry(path):
    set_value("limits.per_request", 250, path)
    set_value("limits.per_request", 1000, path)
    assert path.read_text() == ""


def test_set_rejects_unknown_name(path):
    with pytest.raises(SettingsError, match="unknown setting 'limits.per_reqest'"):
        set_value("limits.per_reqest", 5, path)
    assert not path.exists()


@pytest.mark.parametrize(("name", "value"), [
    ("limits.per_request", 0), ("limits.per_request", "many"), ("limits.per_request", True),
    ("surveillance.hour", 24), ("surveillance.day", "Sunday"), ("data.folder", "srv/data"),
    ("database.address", "sqlite:///x"), ("grobid.address", "localhost:8070"),
    ("sources.contact_email", "not an address"), ("backup.days_kept", "3.5"),
])
def test_set_rejects_bad_value(path, name, value):
    with pytest.raises(SettingsError, match=f"{name} must be"):
        set_value(name, value, path)
    assert not path.exists()


def test_a_file_with_an_unknown_name_or_a_bad_value_is_refused(path):
    path.parent.mkdir()
    path.write_text("[limits]\nper_reqest = 5\n")
    with pytest.raises(SettingsError, match="unknown setting 'limits.per_reqest'"):
        load(path)
    path.write_text("[surveillance]\nhour = 99\n")
    with pytest.raises(SettingsError, match="surveillance.hour must be"):
        load(path)
    path.write_text("[limits\n")
    with pytest.raises(SettingsError, match="not valid TOML"):
        load(path)


def test_show_masks_secrets(path):
    set_value("sources.springer_api_key", "SN-SECRET", path)
    set_value("database.address", "postgresql://scriptorium:hunter2@db.example/scriptorium", path)
    shown = {name: value for name, value, _ in show(path)}
    assert shown["sources.springer_api_key"] == "set"
    assert shown["sources.s2_api_key"] == "not set"
    assert shown["database.address"] == "postgresql://scriptorium:***@db.example/scriptorium"
    assert "SN-SECRET" not in str(show(path)) and "hunter2" not in str(show(path))
    assert load(path)["sources.springer_api_key"] == "SN-SECRET"


def test_every_setting_has_an_explanation():
    assert all(len(s.explanation) > 20 and s.explanation.endswith(".") for s in SETTINGS)
    assert [name for name, _, _ in show("/nonexistent/settings.toml")] == [s.name for s in SETTINGS]
    assert len({s.name for s in SETTINGS}) == len(SETTINGS)


def test_every_default_passes_its_own_check():
    assert all(settings._valid(s, s.default) == s.default for s in SETTINGS)


def test_the_file_is_readable_by_its_owner_only(path):
    set_value("sources.s2_api_key", "k", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_values_with_quotes_survive_and_line_breaks_are_refused(path):
    set_value("sources.s2_api_key", 'a"b\\c', path)
    assert load(path)["sources.s2_api_key"] == 'a"b\\c'
    with pytest.raises(SettingsError):
        set_value("sources.s2_api_key", "a\nb", path)


def test_the_path_comes_from_the_environment(path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_SETTINGS", str(path))
    set_value("embedding.batch_size", 64)
    assert load()["embedding.batch_size"] == 64 and path.exists()


def test_a_section_gives_the_short_names(path):
    set_value("sources.elsevier_api_key", "E", path)
    keys = load(path).section("sources")
    assert keys["elsevier_api_key"] == "E" and keys["springer_api_key"] == ""


def test_the_database_address_can_come_from_the_environment(path, monkeypatch):
    set_value("database.address", "postgresql:///from_file", path)
    monkeypatch.setenv("SCRIPTORIUM_DATABASE_ADDRESS", "postgresql://scriptorium:pw@db/scriptorium")
    assert load(path)["database.address"] == "postgresql://scriptorium:pw@db/scriptorium"
    shown = dict((name, value) for name, value, _ in show(path))
    assert shown["database.address"] == "postgresql://scriptorium:***@db/scriptorium"
    monkeypatch.setenv("SCRIPTORIUM_DATABASE_ADDRESS", "mysql://nope")
    with pytest.raises(SettingsError, match="database.address must be"):
        load(path)


def test_the_grobid_address_can_come_from_the_environment(path, monkeypatch):
    monkeypatch.setenv("SCRIPTORIUM_GROBID_ADDRESS", "http://grobid:8070")
    assert settings.load(path)["grobid.address"] == "http://grobid:8070"
    monkeypatch.setenv("SCRIPTORIUM_GROBID_ADDRESS", "grobid:8070")
    with pytest.raises(settings.SettingsError, match="grobid.address"):
        settings.load(path)
