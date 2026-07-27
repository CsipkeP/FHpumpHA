"""Translation coverage.

Entity names come from the translation files, keyed by the register key.  Home
Assistant has no fallback for a missing one: the entity silently takes the
device's name instead.  These tests make that impossible to ship.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fujitsu_waterstage.const import Control, WriteLevel
from fujitsu_waterstage.discovery import assign_controls, select_registers
from fujitsu_waterstage.registers import RegisterMap

PACKAGE = (
    Path(__file__).resolve().parents[1] / "custom_components" / "fujitsu_waterstage"
)
FILES = ("strings.json", "translations/en.json", "translations/hu.json")

#: Every configuration an installation can end up in.
ROOM_SENSOR_CASES = ([], ["heating_circuit_1"], ["heating_circuit_1", "heating_circuit_2"])


def load(name: str) -> dict:
    return json.loads((PACKAGE / name).read_text(encoding="utf-8"))


def required(register_map: RegisterMap) -> set[tuple[str, str]]:
    """(platform, translation key) for every entity that can ever exist."""
    wanted: set[tuple[str, str]] = set()
    for level in WriteLevel:
        for rooms in ROOM_SENSOR_CASES:
            selected = select_registers(register_map, write_level=level)
            controls = assign_controls(
                selected, write_level=level, room_sensors=rooms
            )
            for key, kinds in controls.items():
                for kind in kinds:
                    if kind is Control.CLIMATE:
                        continue  # keyed by circuit, checked separately
                    if kind is Control.WATER_HEATER:
                        continue
                    wanted.add((kind.value, key))
    wanted.add(("climate", "heating_circuit_1"))
    wanted.add(("climate", "heating_circuit_2"))
    wanted.add(("water_heater", "domestic_hot_water"))
    return wanted


@pytest.mark.parametrize("filename", FILES)
class TestFiles:
    def test_is_valid_json(self, filename: str) -> None:
        assert load(filename)

    def test_every_entity_has_a_name(
        self, filename: str, register_map: RegisterMap
    ) -> None:
        entities = load(filename)["entity"]
        missing = [
            (platform, key)
            for platform, key in sorted(required(register_map))
            if key not in entities.get(platform, {})
            or not entities[platform][key].get("name")
        ]
        assert not missing, f"{filename} is missing {len(missing)}: {missing[:5]}"

    def test_no_stale_entries(self, filename: str, register_map: RegisterMap) -> None:
        """A renamed register must not leave a dead translation behind."""
        entities = load(filename)["entity"]
        wanted = required(register_map)
        extra = [
            (platform, key)
            for platform, keys in entities.items()
            for key in keys
            if (platform, key) not in wanted
        ]
        assert not extra, f"{filename} has {len(extra)} unused: {extra[:5]}"

    def test_the_config_flow_is_covered(self, filename: str) -> None:
        data = load(filename)
        assert set(data["config"]["error"]) == {
            "cannot_connect",
            "unknown_device",
            "unknown",
        }
        options = data["options"]["step"]["init"]["data"]
        assert {"blocks", "room_sensors", "write_level"} <= set(options)


class TestConsistency:
    def test_strings_and_english_match(self) -> None:
        """``strings.json`` is what hassfest checks; en.json is what users get."""
        assert load("strings.json") == load("translations/en.json")

    def test_hungarian_has_the_same_shape(self) -> None:
        english, hungarian = load("translations/en.json"), load("translations/hu.json")
        assert _shape(english) == _shape(hungarian)

    def test_hungarian_is_actually_translated(self) -> None:
        """Guard against a copy of the English file being committed."""
        english = load("translations/en.json")["entity"]["sensor"]
        hungarian = load("translations/hu.json")["entity"]["sensor"]
        same = [
            key
            for key in english
            if english[key]["name"] == hungarian[key]["name"]
        ]
        # A handful of names are identical in both languages by nature.
        assert len(same) < 10, same


def _shape(data: dict) -> set[str]:
    """Every leaf path of a nested dict."""
    paths: set[str] = set()

    def walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}")
        else:
            paths.add(prefix)

    walk(data, "")
    return paths
