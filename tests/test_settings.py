"""Settings: validation, secret handling, and the SCHEMA/DEFAULTS invariant."""

import pytest

from scriptorium.config import Config
from scriptorium.settings import (
    _INT_BOUNDS,
    DEFAULTS,
    SCHEMA,
    load_settings,
    mask_settings,
    save_settings,
    secret_keys,
)

KINDS = {"onoff", "int", "model", "text", "textarea", "secret", "choice"}


def _config(tmp_path):
    return Config(index_dir=tmp_path)


def test_schema_and_defaults_agree():
    for section, spec in SCHEMA.items():
        schema_keys = [f["key"] for f in spec["fields"]]
        # Display order is free; the sets must match exactly.
        assert set(schema_keys) == set(DEFAULTS[section]), section
        assert len(schema_keys) == len(set(schema_keys)), section
        for f in spec["fields"]:
            assert f["kind"] in KINDS, f
            if f["kind"] in ("int", "onoff"):
                assert f["key"] in _INT_BOUNDS, f
            if f["kind"] == "choice":
                assert DEFAULTS[section][f["key"]] in f["options"], f
    assert set(SCHEMA) == set(DEFAULTS)


def test_defaults_returned_when_no_file(tmp_path):
    assert load_settings(_config(tmp_path)) == DEFAULTS


def test_save_validates_bounds(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(ValueError):
        save_settings(config, {"surveillance": {"nightly_hour": 99}})
    with pytest.raises(ValueError):
        save_settings(config, {"surveillance": {"model_appraise": "gpt4"}})


def test_unknown_keys_dropped(tmp_path):
    config = _config(tmp_path)
    saved = save_settings(config, {"surveillance": {"bogus": 1}, "nonsense": {}})
    assert "bogus" not in saved["surveillance"]
    assert "nonsense" not in saved


def test_secret_set_kept_and_cleared(tmp_path):
    config = _config(tmp_path)
    assert ("discovery", "s2_api_key") in secret_keys()

    save_settings(config, {"discovery": {"s2_api_key": "real-value"}})
    assert load_settings(config)["discovery"]["s2_api_key"] == "real-value"
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600

    # Blank and sentinel saves keep the stored key (the panel's behaviour).
    save_settings(config, {"discovery": {"s2_api_key": ""}})
    save_settings(config, {"discovery": {"s2_api_key": "__set__"}})
    assert load_settings(config)["discovery"]["s2_api_key"] == "real-value"

    save_settings(config, {"discovery": {"s2_api_key": "__clear__"}})
    assert load_settings(config)["discovery"]["s2_api_key"] == ""


def test_mask_never_returns_secret(tmp_path):
    config = _config(tmp_path)
    save_settings(config, {"discovery": {"s2_api_key": "real-value"}})
    masked = mask_settings(load_settings(config))
    assert masked["discovery"]["s2_api_key"] == "__set__"
    assert "real-value" not in str(masked)
    assert mask_settings(DEFAULTS)["discovery"]["s2_api_key"] == ""


def test_choice_values_are_checked(tmp_path):
    cfg = Config(index_dir=tmp_path)
    assert save_settings(cfg, {"zotero": {"bibtex_format": "bibtex"}})["zotero"]["bibtex_format"] == "bibtex"
    with pytest.raises(ValueError, match="must be one of"):
        save_settings(cfg, {"zotero": {"bibtex_format": "ris"}})
