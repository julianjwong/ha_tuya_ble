"""The Tuya BLE integration."""

from __future__ import annotations

from dataclasses import dataclass, field

import logging
from typing import Any, Callable

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

TuyaBLESwitchGetter = (
    Callable[["TuyaBLESwitch", TuyaBLEProductInfo], bool | None] | None
)

TuyaBLESwitchIsAvailable = Callable[["TuyaBLESwitch", TuyaBLEProductInfo], bool] | None

TuyaBLESwitchSetter = Callable[["TuyaBLESwitch", TuyaBLEProductInfo, bool], None] | None


@dataclass
class TuyaBLESwitchMapping:
    """Model a DP, description and default values"""

    dp_id: int
    description: SwitchEntityDescription
    force_add: bool = True
    dp_type: TuyaBLEDataPointType | None = None
    bitmap_mask: bytes | None = None
    is_available: TuyaBLESwitchIsAvailable = None
    getter: TuyaBLESwitchGetter = None
    setter: TuyaBLESwitchSetter = None


def is_fingerbot_in_program_mode(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo
) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 2
    return result


def is_fingerbot_in_switch_mode(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo
) -> bool:
    result: bool = True
    if product.fingerbot:
        datapoint = self._device.datapoints[product.fingerbot.mode]
        if datapoint:
            result = datapoint.value == 1
    return result


def is_water_valve_in_switch_mode(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo
) -> bool:
    result: bool = False
    if product.watervalve:
        result = True
    return result


def get_fingerbot_program_repeat_forever(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo
) -> bool | None:
    result: bool | None = None
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and isinstance(datapoint.value, bytes):
            repeat_count = int.from_bytes(datapoint.value[0:2], "big")
            result = repeat_count == 0xFFFF
    return result


def set_fingerbot_program_repeat_forever(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo, value: bool
) -> None:
    if product.fingerbot and product.fingerbot.program:
        datapoint = self._device.datapoints[product.fingerbot.program]
        if datapoint and isinstance(datapoint.value, bytes):
            new_value = (
                int.to_bytes(0xFFFF if value else 1, 2, "big") + datapoint.value[2:]
            )
            self._hass.create_task(datapoint.set_value(new_value))


def set_16wgjvck_water_valve(
    self: TuyaBLESwitch, product: TuyaBLEProductInfo, value: bool
) -> None:
    if value:
        dp_11_val = 60
        dp15 = self._device.datapoints[15]
        dp11 = self._device.datapoints[11]
        if dp15 and dp15.value:
            dp_11_val = int(dp15.value)
        elif dp11 and dp11.value:
            dp_11_val = int(dp11.value)
        if dp_11_val <= 0:
            dp_11_val = 60
        dp_2_val = 100
        dp2 = self._device.datapoints[2]
        if dp2 and dp2.value is not None:
            dp_2_val = int(dp2.value)
        if dp_2_val <= 0:
            dp_2_val = 100
        self._device.datapoints.get_or_create(1, TuyaBLEDataPointType.DT_BOOL, True)
        self._device.datapoints.get_or_create(2, TuyaBLEDataPointType.DT_VALUE, dp_2_val)
        self._device.datapoints.get_or_create(11, TuyaBLEDataPointType.DT_VALUE, dp_11_val)
        dp_updates = {1: True, 2: dp_2_val, 11: dp_11_val}
        self._hass.create_task(self._device.set_multiple_values(dp_updates))
    else:
        self._device.datapoints.get_or_create(1, TuyaBLEDataPointType.DT_BOOL, False)
        self._hass.create_task(self._device.set_multiple_values({1: False}))


@dataclass
class TuyaBLEFingerbotSwitchMapping(TuyaBLESwitchMapping):
    description: SwitchEntityDescription = field(
        default_factory=lambda: SwitchEntityDescription(
            key="switch",
            device_class=SwitchDeviceClass.SWITCH,
        )
    )
    is_available: TuyaBLESwitchIsAvailable = is_fingerbot_in_switch_mode


@dataclass
class TuyaBLEWaterValveSwitchMapping(TuyaBLESwitchMapping):
    description: SwitchEntityDescription = field(
        default_factory=lambda: SwitchEntityDescription(
            key="water_valve",
            icon="mdi:valve",
        )
    )
    is_available: TuyaBLESwitchIsAvailable = is_water_valve_in_switch_mode


@dataclass
class TuyaLockMotorStateMapping(TuyaBLESwitchMapping):
    description: SwitchEntityDescription = field(
        default_factory=lambda: SwitchEntityDescription(
            key="lock_motor_state",
        )
    )


@dataclass
class TuyaBLEWaterValveWeatherSwitchMapping(TuyaBLESwitchMapping):
    description: SwitchEntityDescription = field(
        default_factory=lambda: SwitchEntityDescription(
            key="weather_switch",
            icon="mdi:cloud-question",
        )
    )


@dataclass
class TuyaBLEReversePositionsMapping(TuyaBLESwitchMapping):
    description: SwitchEntityDescription = field(
        default_factory=lambda: SwitchEntityDescription(
            key="reverse_positions",
            icon="mdi:arrow-up-down-bold",
            entity_category=EntityCategory.CONFIG,
        )
    )
    is_available: TuyaBLESwitchIsAvailable = is_fingerbot_in_switch_mode


@dataclass
class TuyaBLECategorySwitchMapping:
    """Models a dict of products and their mappings"""

    products: dict[str, list[TuyaBLESwitchMapping]] | None = None
    mapping: list[TuyaBLESwitchMapping] | None = None


mapping: dict[str, TuyaBLECategorySwitchMapping] = {
    "sfkzq": TuyaBLECategorySwitchMapping(products={}),
    "co2bj": TuyaBLECategorySwitchMapping(products={}),
    "ms": TuyaBLECategorySwitchMapping(
        products={
            **dict.fromkeys(
                ["ludzroix", "isk2p555", "gumrixyt", "sidhzylo", "7a4xvbtt"],
                [],
            ),
            **dict.fromkeys(
                ["uamrw6h3", "mqc2hevy"],
                [],
            ),
            **dict.fromkeys(
                ["6fibxtph", "99gv5nmz"],
                [
                    TuyaBLESwitchMapping(
                        dp_id=33,
                        description=SwitchEntityDescription(
                            key="automatic_lock",
                            icon="mdi:lock-clock",
                            entity_category=EntityCategory.CONFIG,
                        ),
                    ),
                ],
            ),
            "a6nttc41": [],
        },
    ),
    "szjqr": TuyaBLECategorySwitchMapping(products={}),
    "jtmspro": TuyaBLECategorySwitchMapping(
        products={
            **dict.fromkeys(
                ["stugc8dl", "xicdxood"],
                [
                    TuyaBLESwitchMapping(
                        dp_id=33,
                        description=SwitchEntityDescription(
                            key="automatic_lock",
                            icon="mdi:lock-clock",
                            entity_category=EntityCategory.CONFIG,
                        ),
                    ),
                ],
            ),
            "kholoaew": [
                TuyaBLESwitchMapping(
                    dp_id=33,
                    description=SwitchEntityDescription(
                        key="automatic_lock",
                        icon="mdi:lock-clock",
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
            "ajk32biq": [
                TuyaBLESwitchMapping(
                    dp_id=33,
                    description=SwitchEntityDescription(
                        key="automatic_lock",
                        icon="mdi:lock-clock",
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
            "yfqp0shy": [
                TuyaBLESwitchMapping(
                    dp_id=33,
                    description=SwitchEntityDescription(
                        key="automatic_lock",
                        icon="mdi:lock-clock",
                        entity_category=EntityCategory.CONFIG,
                    ),
                ),
            ],
        },
    ),
    "kg": TuyaBLECategorySwitchMapping(products={}),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLESwitchMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
    if category is not None and category.mapping is not None:
        return category.mapping
    return []


class TuyaBLESwitch(TuyaBLEEntity, SwitchEntity):
    """Representation of a Tuya BLE switch."""

    platform = Platform.SWITCH

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLESwitchMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

    @property
    def is_on(self) -> bool | None:
        if self._mapping.getter is not None:
            return self._mapping.getter(self, self._product)
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint is None:
            return None
        if self._mapping.bitmap_mask is not None and isinstance(datapoint.value, (bytes, bytearray)):
            return bool(bytes(a & b for a, b in zip(datapoint.value, self._mapping.bitmap_mask)))
        return bool(datapoint.value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._mapping.setter is not None:
            self._mapping.setter(self, self._product, True)
            return
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            self._mapping.dp_type or TuyaBLEDataPointType.DT_BOOL,
            True,
        )
        await datapoint.set_value(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._mapping.setter is not None:
            self._mapping.setter(self, self._product, False)
            return
        datapoint = self._device.datapoints.get_or_create(
            self._mapping.dp_id,
            self._mapping.dp_type or TuyaBLEDataPointType.DT_BOOL,
            False,
        )
        await datapoint.set_value(False)

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
    """Set up the Tuya BLE switches."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLESwitch] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(mapping.dp_id, mapping.dp_type):
            entities.append(
                TuyaBLESwitch(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
