"""Unit tests for the synthetic data generator and its production guard (Task 8.2)."""

from __future__ import annotations

import pytest

from backend.synthetic import generator


def test_generate_dataset_raises_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(RuntimeError):
        generator.generate_dataset(work_package_count=1, units_per_package=1, work_types=["Fiber"])


def test_generate_dataset_succeeds_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    dataset = generator.generate_dataset(
        work_package_count=2, units_per_package=3, work_types=["Fiber", "Copper"]
    )
    assert len(dataset["work_packages"]) == 2
    assert len(dataset["work_units"]) == 6  # 2 packages x 3 units
    # Both Fiber and Copper packages get a MaterialRequirement (AS-009).
    assert len(dataset["material_requirements"]) == 2


def test_generate_dataset_default_env_is_not_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    dataset = generator.generate_dataset(
        work_package_count=1, units_per_package=1, work_types=["Metronome"]
    )
    assert len(dataset["work_packages"]) == 1
    # Metronome has no material requirement in V1 (AS-009).
    assert dataset["material_requirements"] == []


def test_factories_raise_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")
    with pytest.raises(RuntimeError):
        generator.make_work_package()
    with pytest.raises(RuntimeError):
        generator.make_work_unit(work_package_id="wp-1")
    with pytest.raises(RuntimeError):
        generator.make_material_requirement(work_package_id="wp-1")


def test_synthetic_identifiers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    wp = generator.make_work_package()
    assert wp["site"] == generator.SYNTHETIC_SITE
    assert wp["rack_position"] == generator.SYNTHETIC_RACK


def test_material_requirement_total_meters_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    mr = generator.make_material_requirement(
        work_package_id="wp-1", cable_length_m=50.0, qty_required=4
    )
    assert mr["total_meters_required"] == 200.0


def test_generate_dataset_empty_work_types_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    with pytest.raises(ValueError):
        generator.generate_dataset(work_package_count=1, units_per_package=1, work_types=[])
