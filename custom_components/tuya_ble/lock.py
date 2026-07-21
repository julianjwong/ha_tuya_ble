"""The Tuya BLE integration."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.lock import (
    LockEntity,
    LockEntityDescription,
    LockEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DPCode
from .devices import (
    TuyaBLECoordinator,
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

# Product IDs that need special lock-state handling and raw unlock support.
_SPECIAL_PRODUCT_IDS = {"zyvo0vlb"}

# Validated raw DP71 unlock payloads.
_RAW_UNLOCK_DP71_PAYLOAD = {
    "zyvo0vlb": bytes.fromhex("a4a4a4a43439333236323630016a4784cf000000"),
}

# Timing tuned for this device's observed BLE behaviour: reconnects alone
# have taken over a minute before the command was even sent, and motor-state
# reports (dpid47) bounce during the unlock cycle instead of holding steady.
_COMMAND_TIMEOUT = 180.0
_POST_UNLOCK_DISPLAY_HOLD = 120.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE lock."""
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
        """Initialize the lock."""
        super().__init__(
            hass,
            coordinator,
            device,
            product,
            LockEntityDescription(key="lock", name=product.name),
        )
        self._attr_supported_features = LockEntityFeature.OPEN

        self._special_handling = device.product_id in _SPECIAL_PRODUCT_IDS
        self._raw_unlock_payload = _RAW_UNLOCK_DP71_PAYLOAD.get(device.product_id)

        # None = unknown/unavailable. True/False = confirmed state.
        self._last_confirmed_locked: bool | None = None
        self._display_hold_until: float = 0.0

        self._pending_target_locked: bool | None = None
        self._pending_command_label: str | None = None
        self._command_started: float | None = None
        self._command_timed_out: bool = False
        self._command_timeout_unsub = None

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dpid is None:
            return self._last_confirmed_locked

        if not self._special_handling:
            motor_state = self._device.datapoints.get_or_create(
                dpid, TuyaBLEDataPointType.DT_BOOL, False
            )
            if not motor_state:
                return None
            return not motor_state.value

        # A timed-out command leaves the state unknown rather than trusting
        # a possibly stale/contradictory raw report.
        if self._command_timed_out:
            return None

        if self._pending_target_locked is not None:
            # Still mid-command: report the last confirmed state if we have
            # one, otherwise unknown (never guess from a noisy raw report).
            return self._last_confirmed_locked

        return self._last_confirmed_locked

    @property
    def available(self) -> bool:
        """Return False while state is unknown after a timed-out command."""
        base_available = super().available
        if self._special_handling and self._command_timed_out:
            return False
        return base_available

    @property
    def is_unlocking(self) -> bool | None:
        """Return true if the lock is unlocking."""
        if not self._special_handling:
            return None
        return self._pending_target_locked is False

    @property
    def is_locking(self) -> bool | None:
        """Return true if the lock is locking."""
        if not self._special_handling:
            return None
        return self._pending_target_locked is True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._special_handling:
            motor_dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
            for update in self.coordinator.last_updates or []:
                if motor_dpid is not None and update.id == motor_dpid:
                    self._process_motor_state_update(bool(update.value))

        super()._handle_coordinator_update()

    def _set_confirmed_state(self, locked: bool) -> None:
        """Persist a confirmed lock state."""
        self._last_confirmed_locked = locked
        self._command_timed_out = False

        if not locked:
            self._display_hold_until = time.monotonic() + _POST_UNLOCK_DISPLAY_HOLD
        else:
            self._display_hold_until = 0.0

        self.async_write_ha_state()

    def _process_motor_state_update(self, raw_value: bool) -> None:
        """Process motor state reports for noisy lock models."""
        now = time.monotonic()
        locked = not raw_value

        _LOGGER.debug(
            "%s (%s) motor-state update raw=%s locked=%s pending=%s",
            self._device.address,
            self._device.product_id,
            raw_value,
            locked,
            self._pending_command_label,
        )

        if self._pending_target_locked is not None:
            # This device's motor-state reports bounce during travel, so the
            # first report matching the target is treated as confirmation
            # rather than waiting for a stable dwell that never arrives.
            if locked == self._pending_target_locked:
                _LOGGER.debug(
                    "%s (%s) %s confirmed locked=%s",
                    self._device.address,
                    self._device.product_id,
                    self._pending_command_label,
                    locked,
                )
                self._set_confirmed_state(locked)
                self._complete_pending_command()
            return

        # No pending command: ignore contradictory "locked" blips during the
        # post-unlock display hold to avoid flicker back to Locked.
        if locked and now < self._display_hold_until:
            _LOGGER.debug(
                "%s (%s) ignoring contradictory locked report during post-unlock hold",
                self._device.address,
                self._device.product_id,
            )
            return

        self._set_confirmed_state(locked)

    def _arm_pending_command(self, target_locked: bool, label: str) -> None:
        """Arm a pending command, overriding any stuck pending command."""
        if not self._special_handling:
            return

        if self._pending_target_locked is not None:
            _LOGGER.debug(
                "%s (%s) overriding stuck pending %s with new %s request",
                self._device.address,
                self._device.product_id,
                self._pending_command_label,
                label,
            )

        self._pending_target_locked = target_locked
        self._pending_command_label = label
        self._command_started = time.monotonic()
        self._command_timed_out = False

        if self._command_timeout_unsub is not None:
            self._command_timeout_unsub()

        self._command_timeout_unsub = async_call_later(
            self.hass, _COMMAND_TIMEOUT, self._handle_command_timeout
        )
        self.async_write_ha_state()

    @callback
    def _handle_command_timeout(self, _now: Any = None) -> None:
        """Handle command timeout."""
        if self._pending_target_locked is None:
            return

        _LOGGER.warning(
            "%s (%s) timed out waiting for %s to complete after %.0fs; "
            "reporting state as unknown rather than trusting a stale reading",
            self._device.address,
            self._device.product_id,
            self._pending_command_label,
            _COMMAND_TIMEOUT,
        )
        self._command_timed_out = True
        self._complete_pending_command()
        self.async_write_ha_state()

    def _complete_pending_command(self) -> None:
        """Clear pending command state."""
        if self._command_timeout_unsub is not None:
            self._command_timeout_unsub()
            self._command_timeout_unsub = None

        self._pending_target_locked = None
        self._pending_command_label = None
        self._command_started = None

    async def _send_raw_unlock(self) -> bool:
        """Send validated raw DP71 unlock payload if available."""
        if not self._raw_unlock_payload:
            return False

        dp71 = self._device.datapoints.get_or_create(
            71, TuyaBLEDataPointType.DT_RAW, b""
        )
        if not dp71:
            return False

        await dp71.set_value(self._raw_unlock_payload)
        return True

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        self._arm_pending_command(True, "lock")

        dpid = self.find_dpid(DPCode.MANUAL_LOCK)
        if dpid is None:
            self._complete_pending_command()
            return

        manual_lock = self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, True
        )
        if not manual_lock:
            self._complete_pending_command()
            return

        try:
            await manual_lock.set_value(True)
        except Exception:
            _LOGGER.warning(
                "%s (%s) lock write failed; leaving command pending for retry/timeout",
                self._device.address,
                self._device.product_id,
            )

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        self._arm_pending_command(False, "unlock")

        try:
            if await self._send_raw_unlock():
                return
        except Exception:
            _LOGGER.warning(
                "%s (%s) raw unlock write failed; leaving command pending for retry/timeout",
                self._device.address,
                self._device.product_id,
            )
            return

        dpid = self.find_dpid(DPCode.MANUAL_LOCK)
        if dpid is None:
            self._complete_pending_command()
            return

        manual_lock = self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        )
        if not manual_lock:
            self._complete_pending_command()
            return

        try:
            await manual_lock.set_value(False)
        except Exception:
            _LOGGER.warning(
                "%s (%s) unlock write failed; leaving command pending for retry/timeout",
                self._device.address,
                self._device.product_id,
            )

    async def async_open(self, **kwargs: Any) -> None:
        """Open the lock."""
        await self.async_unlock(**kwargs)
