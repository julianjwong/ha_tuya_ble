from __future__ import annotations

from typing import Any

from homeassistant.components.lock import (
    LockEntity,
    LockEntityDescription,
    LockEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DPCode
from .devices import (
    TuyaBLECoordinator,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

ZYVO0VLB_PRODUCT_ID = "zyvo0vlb"
ZYVO0VLB_UNLOCK_DPID = 71
ZYVO0VLB_UNLOCK_PAYLOAD = bytes.fromhex(
    "a4a4a4a43439333236323630016a4784cf000000"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    product = get_device_product_info(data.device)
    if product and product.lock:
        async_add_entities([TuyaBLELock(hass, data.coordinator, data.device, product)])


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    platform = Platform.LOCK

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: TuyaBLECoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
    ) -> None:
        super().__init__(
            hass,
            coordinator,
            device,
            product,
            LockEntityDescription(key="lock", name=product.name),
        )

        self._attr_supported_features = LockEntityFeature.OPEN

    async def _async_unlock_zyvo0vlb(self) -> None:
        """Run the validated zyvo0vlb DP71 unlock flow."""
        dp71 = self._device.datapoints.get_or_create(
            ZYVO0VLB_UNLOCK_DPID,
            TuyaBLEDataPointType.DT_RAW,
            b"",
        )
        if dp71 is not None:
            await dp71.set_value(ZYVO0VLB_UNLOCK_PAYLOAD)

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        if motor_state := self._device.datapoints.get_or_create(
            DPCode.LOCK_MOTOR_STATE, TuyaBLEDataPointType.DT_BOOL, False
        ):
            return not motor_state.value
        return None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        if manual_lock := self._device.datapoints.get_or_create(
            DPCode.MANUAL_LOCK, TuyaBLEDataPointType.DT_BOOL, True
        ):
            await manual_lock.set_value(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        if self._device.product_id == ZYVO0VLB_PRODUCT_ID:
            await self._async_unlock_zyvo0vlb()
            return

        if manual_lock := self._device.datapoints.get_or_create(
            DPCode.MANUAL_LOCK, TuyaBLEDataPointType.DT_BOOL, False
        ):
            await manual_lock.set_value(False)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the covering."""
        if self._device.product_id == ZYVO0VLB_PRODUCT_ID:
            await self._async_unlock_zyvo0vlb()
            return

        if manual_lock := self._device.datapoints.get_or_create(
            DPCode.MANUAL_LOCK, TuyaBLEDataPointType.DT_BOOL, False
        ):
            await manual_lock.set_value(False)
