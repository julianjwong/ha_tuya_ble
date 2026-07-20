"""The Tuya BLE integration."""
from __future__ import annotations

from typing import Any
import logging
import time

from homeassistant.components.lock import (
    LockEntity,
    LockEntityFeature,
    LockEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DPCode
from .devices import (
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    TuyaBLECoordinator,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPoint, TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT = 8.0
UNLOCK_HOLD_SECONDS = 180.0


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
        self._command_in_progress = False
        self._pending_target_locked: bool | None = None
        self._pending_lock_command = False
        self._clear_command_handle = None
        self._command_started_monotonic: float | None = None
        self._command_label: str | None = None
        self._unlock_hold_until_monotonic: float | None = None

    def _log_debug(self, message: str, *args: Any) -> None:
        _LOGGER.debug(
            "[%s %s] " + message,
            self._device.product_id,
            self._device.address,
            *args,
        )

    def _unlock_hold_active(self) -> bool:
        return (
            self._unlock_hold_until_monotonic is not None
            and time.monotonic() < self._unlock_hold_until_monotonic
        )

    def _set_unlock_hold(self) -> None:
        self._unlock_hold_until_monotonic = time.monotonic() + UNLOCK_HOLD_SECONDS
        self._log_debug(
            "unlock hold armed for %.1fs until monotonic=%.3f",
            UNLOCK_HOLD_SECONDS,
            self._unlock_hold_until_monotonic,
        )

    def _clear_unlock_hold(self) -> None:
        if self._unlock_hold_until_monotonic is not None:
            self._log_debug("unlock hold cleared")
        self._unlock_hold_until_monotonic = None

    async def _run_zyvo0vlb_unlock(self) -> None:
        """Run the validated dp71 unlock flow for zyvo0vlb."""
        dp71_value = bytes.fromhex("a4a4a4a43439333236323630016a4784cf000000")
        dp71 = self._device.datapoints.get_or_create(
            71,
            TuyaBLEDataPointType.DT_RAW,
            b"",
        )
        self._log_debug("dp71 unlock write starting payload=%s", dp71_value.hex())
        t0 = time.monotonic()
        if dp71:
            await dp71.set_value(dp71_value)
        self._log_debug("dp71 unlock write finished elapsed=%.3fs", time.monotonic() - t0)

    def _motor_state_locked(self) -> bool | None:
        """Read raw lock state from motor-state datapoint."""
        dp_id = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dp_id is None:
            self._log_debug("no dp id found for LOCK_MOTOR_STATE")
            return None

        motor_state = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, False
        )
        if motor_state is None:
            self._log_debug("motor_state datapoint missing for dp_id=%s", dp_id)
            return None

        locked = not bool(motor_state.value)
        self._log_debug(
            "motor_state read dp_id=%s raw=%s locked=%s",
            dp_id,
            motor_state.value,
            locked,
        )
        return locked

    def _clear_pending_command(self) -> None:
        elapsed = None
        if self._command_started_monotonic is not None:
            elapsed = time.monotonic() - self._command_started_monotonic
        self._log_debug(
            "clearing command state label=%s elapsed=%s pending_target_locked=%s",
            self._command_label,
            f"{elapsed:.3f}s" if elapsed is not None else None,
            self._pending_target_locked,
        )
        self._command_in_progress = False
        self._pending_target_locked = None
        self._pending_lock_command = False
        self._command_started_monotonic = None
        self._command_label = None
        if self._clear_command_handle is not None:
            self._clear_command_handle()
            self._clear_command_handle = None

    def _arm_command_timeout(self) -> None:
        if self._clear_command_handle is not None:
            self._clear_command_handle()
        self._clear_command_handle = async_call_later(
            self._hass, COMMAND_TIMEOUT, self._handle_command_timeout
        )
        self._log_debug("armed command timeout %.1fs", COMMAND_TIMEOUT)

    @callback
    def _handle_command_timeout(self, _now) -> None:
        elapsed = None
        if self._command_started_monotonic is not None:
            elapsed = time.monotonic() - self._command_started_monotonic
        self._log_debug(
            "command timeout label=%s elapsed=%s pending_target_locked=%s unlock_hold_active=%s",
            self._command_label,
            f"{elapsed:.3f}s" if elapsed is not None else None,
            self._pending_target_locked,
            self._unlock_hold_active(),
        )
        self._command_in_progress = False
        self._pending_target_locked = None
        self._pending_lock_command = False
        self._command_started_monotonic = None
        self._command_label = None
        self._clear_command_handle = None
        self.async_write_ha_state()

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        if self._device.product_id == "zyvo0vlb":
            if self._unlock_hold_active():
                self._log_debug("is_locked returning False because unlock hold is active")
                return False
            if self._optimistic_is_locked is not None:
                self._log_debug("is_locked returning optimistic=%s", self._optimistic_is_locked)
                return self._optimistic_is_locked
        locked = self._motor_state_locked()
        self._log_debug("is_locked returning physical=%s", locked)
        return locked

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        self._log_debug(
            "async_lock requested command_in_progress=%s pending_target_locked=%s connected=%s unlock_hold_active=%s",
            self._command_in_progress,
            self._pending_target_locked,
            self._coordinator.connected,
            self._unlock_hold_active(),
        )
        if self._device.product_id == "zyvo0vlb":
            if self._command_in_progress:
                self._log_debug("async_lock ignored because another command is in progress")
                return
            dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
            if dp_id is None:
                self._log_debug("async_lock aborted because MANUAL_LOCK dpid was not found")
                return
            manual_lock = self._device.datapoints.get_or_create(
                dp_id, TuyaBLEDataPointType.DT_BOOL, True
            )
            if manual_lock is not None:
                self._clear_unlock_hold()
                self._command_in_progress = True
                self._pending_target_locked = True
                self._pending_lock_command = True
                self._optimistic_is_locked = False
                self._command_started_monotonic = time.monotonic()
                self._command_label = "lock"
                self._arm_command_timeout()
                self._log_debug("async_lock sending dp_id=%s value=True", dp_id)
                self.async_write_ha_state()
                t0 = time.monotonic()
                await manual_lock.set_value(True)
                self._log_debug("async_lock write finished elapsed=%.3fs", time.monotonic() - t0)
            else:
                self._log_debug("async_lock could not create datapoint for dp_id=%s", dp_id)
            return

        dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if dp_id is None:
            self._log_debug("async_lock aborted because MANUAL_LOCK dpid was not found")
            return
        manual_lock = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, True
        )
        if manual_lock is not None:
            t0 = time.monotonic()
            await manual_lock.set_value(True)
            self._log_debug("generic async_lock write finished elapsed=%.3fs", time.monotonic() - t0)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        self._log_debug(
            "async_unlock requested command_in_progress=%s pending_target_locked=%s connected=%s unlock_hold_active=%s",
            self._command_in_progress,
            self._pending_target_locked,
            self._coordinator.connected,
            self._unlock_hold_active(),
        )
        if self._device.product_id == "zyvo0vlb":
            if self._command_in_progress:
                self._log_debug("async_unlock ignored because another command is in progress")
                return
            self._command_in_progress = True
            self._pending_target_locked = False
            self._pending_lock_command = False
            self._optimistic_is_locked = False
            self._command_started_monotonic = time.monotonic()
            self._command_label = "unlock"
            self._set_unlock_hold()
            self._arm_command_timeout()
            self._log_debug("async_unlock sending validated dp71 payload")
            self.async_write_ha_state()
            await self._run_zyvo0vlb_unlock()
            return

        dp_id = self.find_dpid(DPCode.MANUAL_LOCK)
        if dp_id is None:
            self._log_debug("async_unlock aborted because MANUAL_LOCK dpid was not found")
            return
        manual_lock = self._device.datapoints.get_or_create(
            dp_id, TuyaBLEDataPointType.DT_BOOL, False
        )
        if manual_lock is not None:
            t0 = time.monotonic()
            await manual_lock.set_value(False)
            self._log_debug("generic async_unlock write finished elapsed=%.3fs", time.monotonic() - t0)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the lock."""
        self._log_debug("async_open called")
        await self.async_unlock(**kwargs)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._device.product_id == "zyvo0vlb":
            updates: list[TuyaBLEDataPoint] | None = self._coordinator.last_updates
            motor_dp_id = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
            if updates:
                self._log_debug(
                    "coordinator update connected=%s updates=%s unlock_hold_active=%s",
                    self._coordinator.connected,
                    [(u.id, getattr(u, 'value', None), getattr(u, 'changed_by_device', None)) for u in updates],
                    self._unlock_hold_active(),
                )
            else:
                self._log_debug(
                    "coordinator update with no last_updates connected=%s unlock_hold_active=%s",
                    self._coordinator.connected,
                    self._unlock_hold_active(),
                )
            if updates and motor_dp_id is not None:
                for update in updates:
                    if update.id == motor_dp_id:
                        locked = self._motor_state_locked()
                        elapsed = None
                        if self._command_started_monotonic is not None:
                            elapsed = time.monotonic() - self._command_started_monotonic
                        self._log_debug(
                            "motor-state update matched dp_id=%s raw=%s locked=%s command=%s elapsed=%s unlock_hold_active=%s",
                            motor_dp_id,
                            update.value,
                            locked,
                            self._command_label,
                            f"{elapsed:.3f}s" if elapsed is not None else None,
                            self._unlock_hold_active(),
                        )
                        if self._pending_lock_command and locked is True:
                            self._optimistic_is_locked = True
                            self._clear_unlock_hold()
                            self._clear_pending_command()
                        elif self._pending_lock_command and locked is not True:
                            self._optimistic_is_locked = False
                            self._command_in_progress = False
                            self._pending_target_locked = None
                            self._pending_lock_command = False
                            self._command_started_monotonic = None
                            self._command_label = None
                            self._log_debug("lock command saw non-locked motor update, releasing pending state")
                        elif not self._pending_lock_command and self._unlock_hold_active():
                            self._log_debug(
                                "ignoring motor-state update during unlock hold to avoid false relock"
                            )
                        elif locked is True:
                            self._optimistic_is_locked = True
                            self._clear_unlock_hold()
                        elif locked is False:
                            self._optimistic_is_locked = False
            self.async_write_ha_state()
            return

        super()._handle_coordinator_update()
