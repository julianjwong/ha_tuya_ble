"""The Tuya BLE integration."""
from __future__ import annotations

import logging
import time
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
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, DPCode
from .devices import (
    TuyaBLEData,
    TuyaBLEEntity,
    TuyaBLEProductInfo,
    TuyaBLECoordinator,
    get_device_product_info,
)
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

_DEBOUNCED_PRODUCT_IDS = {"zyvo0vlb"}

_RAW_UNLOCK_DP71_PAYLOAD = {
    "zyvo0vlb": bytes.fromhex("a4a4a4a43439333236323630016a4784cf000000"),
}

# Real-world BLE reconnects on this network regularly take 40-70s+
# (multiple connection-attempt backoffs observed in debug logs), so
# the old 8s/10s windows were far too short and let stale, replayed
# datapoint state overwrite a correct unlocked reading.
_SETTLE_WINDOW = 0.6
_MIN_SETTLE_SAMPLES = 2
_COMMAND_TIMEOUT = 90.0
_POST_UNLOCK_DISPLAY_HOLD = 90.0


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

        self._debounce_enabled = device.product_id in _DEBOUNCED_PRODUCT_IDS
        self._raw_unlock_payload = _RAW_UNLOCK_DP71_PAYLOAD.get(device.product_id)

        self._last_confirmed_locked: bool | None = None
        self._dp47_history: list[tuple[float, bool]] = []
        self._pending_target_locked: bool | None = None
        self._pending_command_label: str | None = None
        self._command_started: float | None = None
        self._command_timeout_unsub = None
        self._display_hold_until: float = 0.0

    @property
    def is_locked(self) -> bool | None:
        """Return true if lock is locked."""
        dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
        if dpid is None:
            return None

        motor_state = self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        )
        if not motor_state:
            return None

        if not self._debounce_enabled:
            return not motor_state.value

        # While a command is in flight, report the optimistic target via
        # is_locking/is_unlocking instead of falling back to a stale
        # confirmed value - this stops the slider reverting to "Locked"
        # when you navigate away mid-command.
        if self._pending_target_locked is not None:
            return self._last_confirmed_locked

        if self._last_confirmed_locked is not None:
            return self._last_confirmed_locked

        return not motor_state.value

    @property
    def is_unlocking(self) -> bool | None:
        if not self._debounce_enabled:
            return None
        return self._pending_target_locked is False

    @property
    def is_locking(self) -> bool | None:
        if not self._debounce_enabled:
            return None
        return self._pending_target_locked is True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._debounce_enabled:
            motor_dpid = self.find_dpid(DPCode.LOCK_MOTOR_STATE)
            for update in self.coordinator.last_updates or []:
                if motor_dpid is not None and update.id == motor_dpid:
                    self._process_motor_state_update(bool(update.value))

        super()._handle_coordinator_update()

    def _process_motor_state_update(self, raw_value: bool) -> None:
        """Debounce a raw motor-state report for debounce-enabled
        products only. Locked == not raw_value, matching is_locked.
        """
        now = time.monotonic()
        locked = not raw_value

        self._dp47_history.append((now, locked))
        self._dp47_history = [
            (t, v) for t, v in self._dp47_history if now - t <= _SETTLE_WINDOW
        ]
        # Require multiple agreeing samples before treating a reading as
        # settled - a lone report (e.g. replayed cached state during a
        # BLE reconnect handshake) is no longer enough on its own.
        settled = (
            len(self._dp47_history) >= _MIN_SETTLE_SAMPLES
            and len({v for _, v in self._dp47_history}) == 1
        )

        _LOGGER.debug(
            "%s (%s) motor-state update raw=%s locked=%s settled=%s pending=%s samples=%d",
            self._device.address,
            self._device.product_id,
            raw_value,
            locked,
            settled,
            self._pending_command_label,
            len(self._dp47_history),
        )

        if self._pending_target_locked is not None:
            if settled and locked == self._pending_target_locked:
                self._complete_pending_command()
                self._set_confirmed_state(locked)
            return

        if locked is True and now < self._display_hold_until:
            _LOGGER.debug(
                "%s (%s) ignoring contradictory locked report during post-unlock hold",
                self._device.address,
                self._device.product_id,
            )
            return

        if settled:
            self._set_confirmed_state(locked)

    def _set_confirmed_state(self, locked: bool) -> None:
        self._last_confirmed_locked = locked
        self._dp47_history = []
        self._display_hold_until = (
            time.monotonic() + _POST_UNLOCK_DISPLAY_HOLD if locked is False else 0.0
        )
        self.async_write_ha_state()

    def _arm_pending_command(self, target_locked: bool, label: str) -> None:
        if not self._debounce_enabled:
            return

        self._pending_target_locked = target_locked
        self._pending_command_label = label
        self._command_started = time.monotonic()
        self._dp47_history = []

        if self._command_timeout_unsub is not None:
            self._command_timeout_unsub()

        self._command_timeout_unsub = async_call_later(
            self.hass, _COMMAND_TIMEOUT, self._handle_command_timeout
        )
        self.async_write_ha_state()

    @callback
    def _handle_command_timeout(self, _now: Any = None) -> None:
        if self._pending_target_locked is None:
            return

        _LOGGER.warning(
            "%s (%s) timed out waiting for %s to complete after %.0fs - "
            "will keep reporting last confirmed state until a real update arrives",
            self._device.address,
            self._device.product_id,
            self._pending_command_label,
            _COMMAND_TIMEOUT,
        )
        self._complete_pending_command()
        self.async_write_ha_state()

    def _complete_pending_command(self) -> None:
        if self._command_timeout_unsub is not None:
            self._command_timeout_unsub()
            self._command_timeout_unsub = None

        self._pending_target_locked = None
        self._pending_command_label = None
        self._command_started = None

    async def _send_raw_unlock(self) -> bool:
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
        dpid = self.find_dpid(DPCode.MANUAL_LOCK)
        if dpid is None:
            return
        if manual_lock := self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, True
        ):
            self._arm_pending_command(target_locked=True, label="lock")
            await manual_lock.set_value(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        self._arm_pending_command(target_locked=False, label="unlock")

        if await self._send_raw_unlock():
            return

        dpid = self.find_dpid(DPCode.MANUAL_LOCK)
        if dpid is None:
            self._complete_pending_command()
            return
        if manual_lock := self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        ):
            await manual_lock.set_value(False)
        else:
            self._complete_pending_command()

    async def async_open(self, **kwargs: Any) -> None:
        """Open the covering."""
        self._arm_pending_command(target_locked=False, label="open")

        if await self._send_raw_unlock():
            return

        dpid = self.find_dpid(DPCode.MANUAL_LOCK)
        if dpid is None:
            self._complete_pending_command()
            return
        if manual_lock := self._device.datapoints.get_or_create(
            dpid, TuyaBLEDataPointType.DT_BOOL, False
        ):
            await manual_lock.set_value(False)
        else:
            self._complete_pending_command()
