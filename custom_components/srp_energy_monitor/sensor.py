"""Sensors for SRP Energy Monitor.

The demand entities are the point of this integration. Home Assistant's
built-in `srp_energy` exposes one kWh total; on a demand price plan the number
that moves the bill is measured in kW, and there are two of them that must not
be confused:

* **Current demand** -- what the portal prints for the cycle in progress. On an
  average-demand plan (E-15, E-16) it is a mean; on a peak plan (E-27) it is a
  maximum. ``demand_is_average`` says which, so nothing downstream has to guess.
* **Billed cycle peak** -- the single highest on-peak half hour of the cycle,
  with the timestamp it happened. On this account those two figures have sat at
  5.6 kW and 12.7 kW simultaneously. Reporting either as the other is the
  mistake this whole project exists to avoid.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONFIGURATION_URL, DOMAIN, MANUFACTURER, MODEL
from .coordinator import SRPConfigEntry, SRPCoordinator, SRPData


@dataclass(frozen=True, kw_only=True)
class SRPSensorDescription(SensorEntityDescription):
    """A sensor plus how to pull its value and attributes out of SRPData."""

    value_fn: Callable[[SRPData], StateType]
    attrs_fn: Callable[[SRPData], dict[str, Any]] | None = None


def _daily(series: str) -> Callable[[SRPData], StateType]:
    def _value(data: SRPData) -> StateType:
        return (data.daily_totals or {}).get(series)

    return _value


SENSORS: tuple[SRPSensorDescription, ...] = (
    SRPSensorDescription(
        key="demand_audit",
        translation_key="demand_audit",
        icon="mdi:check-decagram-outline",
        value_fn=lambda d: (d.demand_audit or {}).get("state", "incomplete"),
        attrs_fn=lambda d: {key: value for key, value in (d.demand_audit or {}).items() if key != "state"},
    ),
    SRPSensorDescription(
        key="current_demand",
        translation_key="current_demand",
        device_class=SensorDeviceClass.POWER,
        # MEASUREMENT is the only state class HA permits with POWER. It is an
        # imperfect fit -- this is a billing figure over a cycle, not an
        # instantaneous reading -- but the alternatives are rejected outright.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value_fn=lambda d: d.demand_kw,
        attrs_fn=lambda d: {
            "is_average": d.demand_is_average,
            "quantity": "cycle mean of daily maxima" if d.demand_is_average else "cycle maximum",
        },
    ),
    SRPSensorDescription(
        key="billed_cycle_peak",
        translation_key="billed_cycle_peak",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=1,
        value_fn=lambda d: d.cycle_peak.on_peak_high_demand_kw if d.cycle_peak else None,
        attrs_fn=lambda d: (
            {
                "occurred_at": (
                    d.cycle_peak.occurred_at.isoformat() if d.cycle_peak.occurred_at else None
                ),
                "cycle_start": d.cycle_peak.cycle_start.isoformat(),
                "cycle_end": d.cycle_peak.cycle_end.isoformat(),
            }
            if d.cycle_peak
            else {}
        ),
    ),
    SRPSensorDescription(
        key="bill_projection",
        translation_key="bill_projection",
        device_class=SensorDeviceClass.MONETARY,
        # MONETARY accepts TOTAL only; MEASUREMENT is rejected. TOTAL is also
        # the honest description -- a projection revises in both directions.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="USD",
        suggested_display_precision=2,
        value_fn=lambda d: d.projection_usd,
    ),
    # The daily figures carry no state_class on purpose. They are the most
    # recent settled day, which is replaced wholesale each morning rather than
    # accumulating, so summing them into long-term statistics would produce
    # nonsense. The real history lives in the external statistics the
    # coordinator writes.
    #
    # The generation entity deliberately retains only the daily on-peak field.
    # Daily off-peak values can be nonzero; hourly portal generation is a
    # separate series with unverified whole-roof physical coverage.
    SRPSensorDescription(
        key="daily_consumption",
        translation_key="daily_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_daily("usage"),
    ),
    SRPSensorDescription(
        key="daily_generation",
        translation_key="daily_generation",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_daily("generation"),
    ),
    SRPSensorDescription(
        key="daily_net",
        translation_key="daily_net",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=_daily("net"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SRPConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SRP sensors."""
    coordinator = entry.runtime_data
    async_add_entities(SRPSensor(coordinator, entry, desc) for desc in SENSORS)


class SRPSensor(CoordinatorEntity[SRPCoordinator], SensorEntity):
    """One value read from the SRP account."""

    entity_description: SRPSensorDescription
    _attr_has_entity_name = True
    _attr_attribution = "Data from Salt River Project"

    def __init__(
        self,
        coordinator: SRPCoordinator,
        entry: SRPConfigEntry,
        description: SRPSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"SRP {entry.title}",
            entry_type=DeviceEntryType.SERVICE,
            manufacturer=MANUFACTURER,
            model=MODEL,
            configuration_url=CONFIGURATION_URL,
        )

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        attrs: dict[str, Any] = {}
        if self.entity_description.attrs_fn:
            attrs.update(self.entity_description.attrs_fn(data))
        if data.latest_day:
            # SRP publishes a day late, so every figure here is as-of a date
            # that is not today. Saying so beats a stale-looking sensor.
            attrs["data_through"] = data.latest_day.isoformat()
        return attrs or None
