"""The Tuya BLE integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import Platform
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)
SIGNAL_STRENGTH_DP_ID = -1
TuyaBLEBinarySensorIsAvailable = (
    Callable[["TuyaBLEBinarySensor", TuyaBLEProductInfo], bool] | None
)


def _bitmap_value_to_int(value: bytes | bytearray | int) -> int:
    """Convert a Tuya bitmap datapoint value to an integer bitfield."""
    if isinstance(value, bytes | bytearray):
        return int.from_bytes(value, "big")
    return int(value)


def door_status_getter(self: "TuyaBLEBinarySensor") -> None:
    datapoint = self._device.datapoints[self._mapping.dp_id]
    if datapoint and datapoint.value is not None:
        if datapoint.value == "open":
            self._attr_is_on = True
        elif datapoint.value == "closed":
            self._attr_is_on = False
        else:
            self._attr_is_on = None


@dataclass
class TuyaBLEBinarySensorMapping:
    """Models a BLE binary sensor"""

    dp_id: int
    description: BinarySensorEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None
    getter: Callable[["TuyaBLEBinarySensor"], None] | None = None
    bit: int | None = None
    is_available: TuyaBLEBinarySensorIsAvailable = None


@dataclass
class TuyaBLECategoryBinarySensorMapping:
    """Maps between a dict of products and the sensors"""

    products: dict[str, list[TuyaBLEBinarySensorMapping]] | None = None
    mapping: list[TuyaBLEBinarySensorMapping] | None = None


mapping: dict[str, TuyaBLECategoryBinarySensorMapping] = {
    "jtmspro": TuyaBLECategoryBinarySensorMapping(
        products={
            "zyvo0vlb": [
                TuyaBLEBinarySensorMapping(
                    dp_id=47,
                    description=BinarySensorEntityDescription(
                        key="lock_motor_state",
                        device_class=BinarySensorDeviceClass.LOCK,
                        entity_category=EntityCategory.DIAGNOSTIC,
                    ),
                ),
                TuyaBLEBinarySensorMapping(
                    dp_id=24,
                    description=BinarySensorEntityDescription(
                        key="doorbell",
                        device_class=BinarySensorDeviceClass.OCCUPANCY,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                    ),
                ),
            ],
        },
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLEBinarySensorMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
    if category is not None and category.mapping is not None:
        return category.mapping
    return []


class TuyaBLEBinarySensor(TuyaBLEEntity, BinarySensorEntity):
    """Representation of a Tuya BLE binary sensor."""

    platform = Platform.BINARY_SENSOR

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLEBinarySensorMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._mapping.getter is not None:
            self._mapping.getter(self)
        else:
            datapoint = self._device.datapoints[self._mapping.dp_id]
            if datapoint:
                if self._mapping.bit is not None and datapoint.value is not None:
                    value = _bitmap_value_to_int(datapoint.value)
                    self._attr_is_on = bool((value >> self._mapping.bit) & 1)
                else:
                    self._attr_is_on = bool(datapoint.value)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        result = super().available
        if result and self._mapping.is_available:
            result = self._mapping.is_available(self, self._product)
        return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLEBinarySensor] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(mapping.dp_id, mapping.dp_type):
            entities.append(
                TuyaBLEBinarySensor(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
