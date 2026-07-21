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

# Timing tuned for noisy BLE proxy/reconnect environments.
_COMMAND_TIMEOUT = 120.0
_STABLE_DWELL = 2.5
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

        self._last_confirmed_locked: bool | None = None
        self._display_hold_until: float = 0.0

        self._pending_target_locked: bool | None = None
        self._pending_command_label: str | None = None
        self._command_started: float | None = None
        self._command_timeout_unsub = None

        self._candidate_state: bool | None = None
        self._candidate_since: float | None = None

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dpid is None:
            return self._last_confirmed_locked

        motor_state = self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        )
        if not motor_state:
            return self._last_confirmed_locked

        raw_locked = not motor_state.value

        if not self._special_handling:
            return raw_locked

        if self._pending_target_locked is not None:
            if self._last_confirmed_locked is not None:
                return self._last_confirmed_locked
            return raw_locked

        if self._last_confirmed_locked is not None:
            return self._last_confirmed_locked

        return raw_locked

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
        self._candidate_state = None
        self._candidate_since = None

        if not locked:
            self._display_hold_until = time.monotonic() + _POST_UNLOCK_DISPLAY_HOLD
        else:
            self._display_hold_until = 0.0

        self.async_write_ha_state()

    def _start_candidate(self, locked: bool) -> None:
        """Start or refresh a candidate state awaiting dwell confirmation."""
        now = time.monotonic()
        if self._candidate_state != locked:
            self._candidate_state = locked
            self._candidate_since = now

    def _candidate_matured(self) -> bool:
        """Return True if the current candidate state has dwelled long enough."""
        if self._candidate_since is None:
            return False
        return (time.monotonic() - self._candidate_since) >= _STABLE_DWELL

    def _process_motor_state_update(self, raw_value: bool) -> None:
        """Process motor state reports for noisy lock models."""
        now = time.monotonic()
        locked = not raw_value

        _LOGGER.debug(
            "%s (%s) motor-state update raw%s locked%s pending%s candidate%s",
            self._device.address,
            self._device.product_id,
            raw_value,
            locked,
            self._pending_command_label,
            self._candidate_state,
        )

        # While pending, only accept success after a stable dwell at target.
        if self._pending_target_locked is not None:
            if locked == self._pending_target_locked:
                self._start_candidate(locked)
                if self._candidate_matured():
                    _LOGGER.debug(
                        "%s (%s) %s reached stable target locked=%s",
                        self._device.address,
                        self._device.product_id,
                        self._pending_command_label,
                        locked,
                    )
                    self._set_confirmed_state(locked)
                    self._complete_pending_command()
            else:
                # Opposite report resets the dwell timer but does not fail the command.
                self._candidate_state = None
                self._candidate_since = None
            return

        # No pending command: require dwell before changing confirmed state.
        # During post-unlock hold, ignore contradictory relock blips.
        if locked and now < self._display_hold_until:
            _LOGGER.debug(
                "%s (%s) ignoring contradictory locked report during post-unlock hold",
                self._device.address,
                self._device.product_id,
            )
            return

        self._start_candidate(locked)
        if self._candidate_matured():
            self._set_confirmed_state(locked)

    def _arm_pending_command(self, target_locked: bool, label: str) -> bool:
        """Arm a pending command; return False if an identical command is already pending."""
        if not self._special_handling:
            return True

        if (
            self._pending_target_locked == target_locked
            and self._pending_command_label == label
        ):
            _LOGGER.debug(
                "%s (%s) ignoring duplicate in-flight %s request",
                self._device.address,
                self._device.product_id,
                label,
            )
            return False

        self._pending_target_locked = target_locked
        self._pending_command_label = label
        self._command_started = time.monotonic()
        self._candidate_state = None
        self._candidate_since = None

        if self._command_timeout_unsub is not None:
            self._command_timeout_unsub()

        self._command_timeout_unsub = async_call_later(
            self.hass, _COMMAND_TIMEOUT, self._handle_command_timeout
        )
        self.async_write_ha_state()
        return True

    @callback
    def _handle_command_timeout(self, _now: Any = None) -> None:
        """Handle command timeout."""
        if self._pending_target_locked is None:
            return

        _LOGGER.warning(
            "%s (%s) timed out waiting for %s to complete after %.0fs",
            self._device.address,
            self._device.product_id,
            self._pending_command_label,
            _COMMAND_TIMEOUT,
        )
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
        self._candidate_state = None
        self._candidate_since = None

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
        if self._special_handling and not self._arm_pending_command(True, "lock"):
            return

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

        await manual_lock.set_value(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        if self._special_handling and not self._arm_pending_command(False, "unlock"):
            return

        if await self._send_raw_unlock():
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

        await manual_lock.set_value(False)

    async def async_open(self, **kwargs: Any) -> None:
        """Open the lock."""
        await self.async_unlock(**kwargs)
