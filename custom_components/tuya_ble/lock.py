"""The Tuya BLE integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import (
    LockEntity,
    LockEntityFeature,
    LockEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DPCode
from .devices import (
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    TuyaBLECoordinator,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE locks."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    product = get_device_product_info(data.device)
    if product and product.lock:
        async_add_entities([TuyaBLELock(hass, data.coordinator, data.device, product)])


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    """Representation of a Tuya BLE lock."""

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
        self._optimistic_is_locked: bool | None = None
        self._pending_lock_command = False

    async def _run_zyvo0vlb_unlock(self) -> None:
        """Run the validated dp71 unlock flow for zyvo0vlb."""
        dp71_value = bytes.fromhex("a4a4a4a43439333236323630016a4784cf000000")
        dp71 = self._device.datapoints.get_or_create(
            71,
            TuyaBLEDataPointType.DT_RAW,
            b"",
        )
        if dp71:
            await dp71.set_value(dp71_value)

    def _motor_state_locked(self) -> bool | None:
        """Read raw lock state from motor-state datapoint."""
        dp_id = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dp_id is None:
            return None

        motor_state = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, False
        )
        if motor_state is None:
            return None

        return not bool(motor_state.value)

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        if self._device.product_id == "zyvo0vlb" and self._optimistic_is_locked is not None:
            return self._optimistic_is_locked
        return self._motor_state_locked()

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if dp_id is None:
            return

        manual_lock = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, True
        )
        if manual_lock is not None:
            if self._device.product_id == "zyvo0vlb":
                self._pending_lock_command = True
            await manual_lock.set_value(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        if self._device.product_id == "zyvo0vlb":
            self._optimistic_is_locked = False
            self._pending_lock_command = False
            self.async_write_ha_state()
            await self._run_zyvo0vlb_unlock()
            return

        dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if dp_id is None:
            return

        manual_lock = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, False
        )
        if manual_lock is not None:
            await manual_lock.set_value(False)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the lock."""
        if self._device.product_id == "zyvo0vlb":
            self._optimistic_is_locked = False
            self._pending_lock_command = False
            self.async_write_ha_state()
            await self._run_zyvo0vlb_unlock()
            return

        dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if dp_id is None:
            return

        manual_lock = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, False
        )
        if manual_lock is not None:
            await manual_lock.set_value(False)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._device.product_id == "zyvo0vlb":
            updates: list[TuyaBLEDataPoint] | None = self._coordinator.last_updates
            motor_dp_id = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
            if updates and motor_dp_id is not None:
                for update in updates:
                    if update.id == motor_dp_id:
                        locked = self._motor_state_locked()
                        if locked is True:
                            self._optimistic_is_locked = True
                            self._pending_lock_command = False
                        elif self._pending_lock_command:
                            self._optimistic_is_locked = False
            self.async_write_ha_state()
            return

        super()._handle_coordinator_update()
