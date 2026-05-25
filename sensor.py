"""Sensor platform for fake_sensor integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
import logging
import threading
from typing import cast

from decoda import PGN, SPN, EncodedValue, ScalarValue
from decoda.exceptions import UnknownReferenceError
from propcache.api import cached_property

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    RestoreEntity,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import GeneratorConfigEntry, GeneratorData
from .client import GeneratorClientEvent
from .const import DOMAIN
from .database import (
    FMI_VALUES,
    DiagnosticValue,
    GeneratorDatabaseAddedEvent,
    GeneratorDatabaseEvent,
    GeneratorDatabaseUnchangedEvent,
    GeneratorDatabaseUpdatedEvent,
    Value,
)
from .dm1 import BlinkDisplayValue, FlashStatus, LampFlashStatus, LampState, LampStatus

_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GeneratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize fake_sensor config entry."""

    config_entry.runtime_data.database.register_event_callback(
        lambda event: process_database_event(
            hass, event, config_entry, async_add_entities
        )
    )

    store_data = config_entry.runtime_data.store_data
    seen_ids = []
    for seen in store_data.get("seen", []):
        if isinstance(seen, list | tuple) and len(seen) == 2:
            seen = tuple(seen)
            if seen not in seen_ids:
                seen_ids.append(seen)
        else:
            _LOGGER.warning("Invalid seen entry in storage: %r", seen)
    store_data["seen"] = seen_ids

    _LOGGER.debug("Restoring seen entities: %r", seen_ids)

    spec = config_entry.runtime_data.client.spec
    for pgn_id, spn_id in seen_ids:
        try:
            pgn = spec.PGNs.get_by_id(pgn_id)
        except UnknownReferenceError:
            _LOGGER.warning("Unknown PGN ID %s in storage, skipping", pgn_id)
            continue

        try:
            spn = spec.SPNs.get_by_id(spn_id)
        except UnknownReferenceError:
            _LOGGER.warning("Unknown SPN ID %s in storage, skipping", spn_id)
            continue

        if config_entry.runtime_data.database.get(pgn, spn):
            continue

        config_entry.runtime_data.database.set(pgn, spn, None)

    # config_entry.runtime_data.client.request_pgn(PGN(65227, "", "", 0, 0, []), -1)

    seen_dtc = list({(v,) for (v,) in store_data.get("seen_dtc", [])})
    known_dtcs: list[DiagnosticValue] = []
    for (spn_id,) in seen_dtc:
        try:
            spn = spec.SPNs.get_by_id(spn_id)
        except UnknownReferenceError:
            _LOGGER.warning("Unknown SPN ID %s in storage, skipping", spn_id)
            continue

        known_dtcs.append(DiagnosticValue(spn, -1, -1, None))

    _LOGGER.debug("Restoring seen diagnostics: %r", known_dtcs)
    config_entry.runtime_data.database.restore_diagnostics(known_dtcs)

    seen_lamp = list({(v,) for (v,) in store_data.get("seen_lamp", [])})
    for (name,) in seen_lamp:
        create_or_set_lamp_sensor_value(
            hass,
            config_entry,
            async_add_entities,
            LampFlashStatus(name, LampStatus.NOT_AVAILABLE_2, FlashStatus.NO_FLASH),
        )

    async_add_entities(
        [
            GeneratorClientStatusSensorEntity(
                config_entry.runtime_data,
                f"{config_entry.entry_id}-client-connected",
            ),
            GeneratorClientLastUpdateEntity(
                config_entry.runtime_data,
                f"{config_entry.entry_id}-last-update",
            ),
        ]
    )


def store_seen_entity(
    config_entry: GeneratorConfigEntry,
    value: Value,
) -> None:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    if "seen" not in store_data:
        store_data["seen"] = []

    seen_ids = store_data["seen"]
    id_tuple = (value.pgn.id, value.spn.id)
    if id_tuple not in seen_ids:
        seen_ids.append(id_tuple)
        config_entry.runtime_data.store.async_delay_save(lambda: store_data, 10)


def was_previous_seen(
    config_entry: GeneratorConfigEntry,
    value: Value,
) -> bool:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    return (value.pgn.id, value.spn.id) in store_data.get("seen", [])


def store_seen_dtc_entity(
    config_entry: GeneratorConfigEntry,
    value: DiagnosticValue,
) -> None:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    if "seen_dtc" not in store_data:
        store_data["seen_dtc"] = []

    seen_ids = list({(v,) for (v,) in store_data.get("seen_dtc", [])})
    id_tuple = (value.spn.id,)
    if id_tuple not in seen_ids:
        seen_ids.append(id_tuple)
        store_data["seen_dtc"] = seen_ids
        config_entry.runtime_data.store.async_delay_save(lambda: store_data, 10)


def was_previous_seen_dtc(
    config_entry: GeneratorConfigEntry,
    value: DiagnosticValue,
) -> bool:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    seen_dtc = list({(v,) for (v,) in store_data.get("seen_dtc", [])})
    return (value.spn.id,) in seen_dtc


def store_seen_lamp_entity(
    config_entry: GeneratorConfigEntry,
    value: LampFlashStatus,
) -> None:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    if "seen_lamp" not in store_data:
        store_data["seen_lamp"] = []

    seen_ids = list({(v,) for (v,) in store_data.get("seen_lamp", [])})
    id_tuple = (value.name,)
    if id_tuple not in seen_ids:
        seen_ids.append(id_tuple)
        store_data["seen_lamp"] = seen_ids
        config_entry.runtime_data.store.async_delay_save(lambda: store_data, 10)


def was_previous_seen_lamp(
    config_entry: GeneratorConfigEntry,
    value: LampFlashStatus,
) -> bool:
    """Store the seen entity in the config entry store."""
    store_data = config_entry.runtime_data.store_data
    seen_lamp = list({(v,) for (v,) in store_data.get("seen_lamp", [])})
    return (value.name,) in seen_lamp


def create_or_set_sensor_value(
    hass: HomeAssistant,
    config_entry: GeneratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    value: Value,
) -> None:
    """Set the value of a sensor entity."""
    unique_id = f"{config_entry.entry_id}-{value.pgn.id}-{value.spn.id}"
    sensor = config_entry.runtime_data.entities.get(unique_id, None)
    if sensor:
        if not isinstance(sensor, GeneratorSensorEntity):
            _LOGGER.warning(
                "Received update for entity %s > %s which is not a generator sensor (got %s)",
                value.pgn.name,
                value.spn.name,
                type(sensor).__name__,
            )
            return
        sensor.set_value(value)
        return

    if hass.states.get(unique_id):
        _LOGGER.error(
            "Entity ID %s already exists but not in registry, skipping",
            unique_id,
        )
        return

    if value.is_available is False and not was_previous_seen(config_entry, value):
        _LOGGER.debug(
            "Not creating entity %s > %s as value is not available",
            value.pgn.name,
            value.spn.name,
        )
        return

    sensor = GeneratorSensorEntity(
        config_entry.runtime_data,
        unique_id,
        value.spn.name,
        value.pgn,
        value.spn,
        value,
    )
    async_add_entities([sensor])
    config_entry.runtime_data.entities[unique_id] = sensor

    # On first-seen, store the entity in the config entry store
    # so we can restore the same sensors on restart
    store_seen_entity(config_entry, value)


def create_or_set_diagnostic_sensor_value(
    hass: HomeAssistant,
    config_entry: GeneratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    dtc: DiagnosticValue,
) -> None:
    """Set the value of a sensor entity."""

    if not dtc.is_available and not was_previous_seen_dtc(config_entry, dtc):
        _LOGGER.debug(
            "Not creating entity for DTC %s as value is not available",
            dtc.spn.name,
        )
        return

    for name, klass in DTC_SENSORS.items():
        unique_id = f"{config_entry.entry_id}-dtc-{dtc.spn.id}-{name.lower()}"
        sensor = config_entry.runtime_data.entities.get(unique_id, None)

        if sensor:
            if not isinstance(sensor, klass):
                _LOGGER.warning(
                    "Received update for DTC %s but entity is not a DTC sensor (got %s)",
                    dtc.spn.name,
                    type(sensor).__name__,
                )
                continue

            _LOGGER.info(
                "Received update for DTC %s, will set value on sensor %s (value=%s)",
                dtc.spn.name,
                sensor.name,
                repr(dtc),
            )
            sensor.set_value(dtc)
            continue

        if hass.states.get(unique_id):
            _LOGGER.error(
                "Entity ID %s already exists but not in registry, skipping",
                unique_id,
            )
            continue

        sensor = cast(
            SensorEntity,
            klass(
                config_entry.runtime_data,
                unique_id,
                name.capitalize(),
                str(dtc.spn.name).capitalize(),
                dtc,
            ),
        )
        async_add_entities([sensor])
        config_entry.runtime_data.entities[unique_id] = sensor

        _LOGGER.info(
            "Received update for DTC %s, created sensor %s (value=%s)",
            dtc.spn.name,
            sensor.name,
            repr(dtc),
        )

    # On first-seen, store the entity in the config entry store
    # so we can restore the same sensors on restart
    store_seen_dtc_entity(config_entry, dtc)


def create_or_set_lamp_sensor_value(
    hass: HomeAssistant,
    config_entry: GeneratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    status: LampFlashStatus,
) -> None:
    """Set the value of a sensor entity."""

    if status.is_unavailable and not was_previous_seen_lamp(config_entry, status):
        _LOGGER.debug(
            "Not creating entity lamp %s as value is not available",
            status.name,
        )
        return

    for name, klass in LAMP_SENSORS.items():
        unique_id = f"{config_entry.entry_id}-lamp-{status.name}-{name.lower()}"
        sensor = config_entry.runtime_data.entities.get(unique_id, None)
        if sensor:
            if not isinstance(sensor, klass):
                _LOGGER.warning(
                    "Received update for lamp state %s which is not a lamp status sensor (got %s)",
                    status.name,
                    type(sensor).__name__,
                )
                continue

            _LOGGER.info(
                "Received update for Lamp %s, will set value on sensor %s (value=%s)",
                status.name,
                sensor.name,
                repr(status),
            )
            sensor.set_value(status)
            continue

        if hass.states.get(unique_id):
            _LOGGER.error(
                "Entity ID %s already exists but not in registry, skipping",
                unique_id,
            )
            continue

        sensor = cast(
            SensorEntity,
            klass(
                config_entry.runtime_data,
                unique_id,
                name,
                status.name,
                status,
            ),
        )
        async_add_entities([sensor])
        config_entry.runtime_data.entities[unique_id] = sensor

        _LOGGER.info(
            "Received update for Lamp %s, created sensor %s (value=%s)",
            status.name,
            sensor.name,
            repr(status),
        )

    # On first-seen, store the entity in the config entry store
    # so we can restore the same sensors on restart
    store_seen_lamp_entity(config_entry, status)


def process_database_event(
    hass: HomeAssistant,
    event: GeneratorDatabaseEvent,
    config_entry: GeneratorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Process a database event."""
    if isinstance(event.value, Value):
        if isinstance(event, GeneratorDatabaseAddedEvent):
            if config_entry.pref_disable_new_entities:
                _LOGGER.info(
                    "New entity %s > %s disabled by configuration",
                    event.value.pgn.name,
                    event.value.spn.name,
                )
                return

            _LOGGER.debug(
                "%s > %s added with value %s",
                event.value.pgn.name,
                event.value.spn.name,
                event.value.display,
            )

            value = event.value
        elif isinstance(event, GeneratorDatabaseUnchangedEvent):
            # _LOGGER.debug(
            #     "%s > %s value unchanged (%s)",
            #     event.value.pgn.name,
            #     event.value.spn.name,
            #     event.value.display,
            # )

            value = event.value
        elif isinstance(event, GeneratorDatabaseUpdatedEvent):
            _LOGGER.debug(
                "%s > %s value updated from %s to %s",
                event.value.pgn.name,
                event.value.spn.name,
                event.old_value.display,
                event.value.display,
            )

            value = event.value
        else:
            _LOGGER.warning("Unknown event type: %s", type(event).__name__)
            return

        with _LOCK:
            create_or_set_sensor_value(hass, config_entry, async_add_entities, value)

    if isinstance(event.value, DiagnosticValue):
        _LOGGER.debug(
            "Processing diagnostic event: %s for DTC %s",
            type(event).__name__,
            event.value.spn.name,
        )
        value = event.value
        create_or_set_diagnostic_sensor_value(
            hass,
            config_entry,
            async_add_entities,
            value,
        )
        return

    if isinstance(event.value, LampState):
        _LOGGER.debug(
            "Processing lamp event: %s for lamp %s",
            type(event).__name__,
            str(event.value),
        )
        value = event.value

        for status in [
            value.protect,
            value.amber_warning,
            value.red_stop,
            value.malfunction,
        ]:
            create_or_set_lamp_sensor_value(
                hass, config_entry, async_add_entities, status
            )
        return


class GeneratorSensorEntity(RestoreSensor, SensorEntity):
    """Class representing a sensor for a specific PGN/SPN."""

    _attr_should_poll = False

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
        name: str,
        pgn: PGN,
        spn: SPN,
        value: Value,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__()
        self._attr_name = name
        self._attr_has_entity_name = True
        self._attr_unique_id = self._unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{gen_data.client.host}:{gen_data.client.port}")},
        )

        self._gen_data = gen_data
        gen_data.client.register_event_callback(self._handle_client_event)

        self._restored_data = None
        self._gen_data = gen_data
        self._pgn = pgn
        self._spn = spn
        self._value = value
        self.update()

    def _handle_client_event(self, event: GeneratorClientEvent) -> None:
        """Handle client events."""
        self.set_connected()

    def set_connected(self) -> None:
        """Set the current value and update the state."""
        self._attr_available = self._gen_data.client.is_connected()
        self.schedule_update_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        self._restored_data = await self.async_get_last_sensor_data()
        if self._restored_data is not None:
            self.update()
            if self._restored_data.native_value is not None:
                _LOGGER.debug("Restored data: %s", self._restored_data.native_value)

    def _units_of_measurement_from_spn(self) -> str | None:
        if isinstance(self._spn.value_decoder, ScalarValue):
            return str(self._spn.value_decoder.units)
        return None

    def _device_class_from_units(self) -> SensorDeviceClass | None:
        """Determine the device class from the units of measurement."""
        if isinstance(self._spn.value_decoder, EncodedValue):
            return SensorDeviceClass.ENUM
        units = self._units_of_measurement_from_spn()
        if units in ("°C", "°F"):
            return SensorDeviceClass.TEMPERATURE
        if units in ("Pa", "kPa", "MPa", "bar", "psi"):
            return SensorDeviceClass.PRESSURE
        if units in ("V", "mV"):
            return SensorDeviceClass.VOLTAGE
        if units in ("A", "mA"):
            return SensorDeviceClass.CURRENT
        if units in ("W", "kW", "MW"):
            return SensorDeviceClass.POWER
        if units in ("Wh", "kWh", "MWh"):
            return SensorDeviceClass.ENERGY
        if units in ("Hz", "kHz"):
            return SensorDeviceClass.FREQUENCY
        return None

    @property
    def entity_description(self) -> SensorEntityDescription:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the entity description."""
        return SensorEntityDescription(
            key=self._unique_id,
            device_class=self._device_class_from_units(),
            native_unit_of_measurement=self._units_of_measurement_from_spn(),
            options=(
                None
                if not isinstance(self._spn.value_decoder, EncodedValue)
                else [
                    v.capitalize() for v in self._spn.value_decoder.encodings.values()
                ]
            ),
            state_class=(
                SensorStateClass.MEASUREMENT
                if isinstance(self._spn.value_decoder, ScalarValue)
                else None
            ),
        )

    def set_value(self, value: Value) -> None:
        """Set the current value and update the state."""
        self._value = value
        self.update()
        self.schedule_update_ha_state()

    def update(self) -> None:
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        """
        if self._value.is_available:
            if isinstance(self._spn.value_decoder, EncodedValue):
                self._attr_native_value = self._value.decoded.capitalize()
            else:
                self._attr_native_value = self._value.decoded
        elif self._restored_data is not None:
            self._attr_native_value = self._restored_data.native_value
        else:
            self._attr_native_value = None

        self._attr_extra_state_attributes = {
            "pgn_id": self._pgn.id,
            "pgn_name": self._pgn.name,
            "pgn_description": self._pgn.description,
            "spn_id": self._spn.id,
            "spn_name": self._spn.name,
            "spn_description": self._spn.description,
            "raw": self._value.raw,
            "display": self._value.display,
            "is_available": self._value.is_available,
            "is_not_available": self._value.is_not_available,
            "is_error_indicator": self._value.is_error_indicator,
            "is_param_specific_indicator": self._value.is_param_specific_indicator,
        }


class GeneratorDiagnosticSensorEntity[T](ABC, RestoreEntity, Entity):
    """Class representing a sensor for a Lamp Status."""

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
        diagnostic_name: str,
        value_name: str,
        value: T,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__()
        self._attr_name = f"{value_name.replace('_', ' ').title()} {diagnostic_name.replace('_', ' ').title()}"
        self._attr_has_entity_name = True
        self._attr_unique_id = self._unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{gen_data.client.host}:{gen_data.client.port}")},
        )

        self._gen_data = gen_data
        self._gen_data.client.register_event_callback(self._handle_client_event)

        self._restored_data = None
        self._gen_data = gen_data
        self._diagnostic_name = diagnostic_name
        self._value_name = value_name
        self._value: T = value

        self.update()
        self._attr_available: bool = self.is_available()

    @abstractmethod
    async def async_get_last_sensor_data(self) -> SensorExtraStoredData | None:
        """Restore native_value and native_unit_of_measurement."""

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        self._restored_data = await self.async_get_last_sensor_data()
        if self._restored_data is not None:
            _LOGGER.info(
                "%s: Restored data: %s",
                self._attr_name,
                repr(self._restored_data.native_value),
            )
        self._attr_available = self.is_available()
        self.update()
        self.schedule_update_ha_state()

    def set_value(self, value: T) -> None:
        """Set the current value and update the state."""
        self._value = value
        self._restored_data = None
        self._attr_available = self.is_available()
        self.update()
        self.schedule_update_ha_state()

    def is_available(self) -> bool:
        """Based on self._value, returns true when the sensor is available."""
        return self._gen_data.client.is_connected()

    def _handle_client_event(self, event: GeneratorClientEvent) -> None:
        """Handle client events."""
        self.set_connected()

    def set_connected(self) -> None:
        """Set the current value and update the state."""
        self._attr_available = self.is_available()
        self.schedule_update_ha_state()

    def update(self) -> None:
        """Set the native value."""


class RestoreBinarySensor(BinarySensorEntity, RestoreEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Mixin class to add restore binary sensor state functionality."""

    @property
    def extra_restore_state_data(self) -> SensorExtraStoredData:
        """Return sensor specific state data to be restored."""
        return SensorExtraStoredData("on" if self.is_on is True else "off", None)

    async def async_get_last_sensor_data(self) -> SensorExtraStoredData | None:
        """Restore native_value and native_unit_of_measurement."""
        if (restored_last_extra_data := await self.async_get_last_extra_data()) is None:
            return None
        return SensorExtraStoredData.from_dict(restored_last_extra_data.as_dict())


class GeneratorDiagnosticTroubleCodeFailureMoodeSensorEntity(  # pyright: ignore[reportIncompatibleVariableOverride]
    RestoreSensor,
    SensorEntity,
    GeneratorDiagnosticSensorEntity[DiagnosticValue],
):
    """Class representing a sensor for a specific Diagnostic Trouble Code."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
        diagnostic_name: str,
        value_name: str,
        value: DiagnosticValue,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__(gen_data, unique_id, diagnostic_name, value_name, value)

        self.entity_description = SensorEntityDescription(
            key=self._unique_id,
            device_class=SensorDeviceClass.ENUM,
            options=list(FMI_VALUES.values()),
        )

    def is_available(self) -> bool:
        """Returns true when lamp status is available."""
        return (
            (
                self._restored_data is not None
                and self._restored_data.native_value in FMI_VALUES.values()
            )
            or (self._value.is_available and self._value.failure_mode_identifier >= 0)
        ) and super().is_available()

    def update(self) -> None:
        """Set the current value."""
        if self._restored_data:
            self._attr_native_value = self._restored_data.native_value

            self._attr_extra_state_attributes = {"restored": True}
        elif self._value.failure_mode_identifier >= 0:
            self._attr_native_value = self._value.display

            self._attr_extra_state_attributes = {
                "spn_id": self._value.spn.id,
                "failure_mode_identifier": self._value.failure_mode_identifier,
            }
        else:
            self._attr_native_value = "Unknown"
            self._attr_extra_state_attributes = {"restored": True}


class GeneratorDiagnosticTroubleCodeOccurencesSensorEntity(  # pyright: ignore[reportIncompatibleVariableOverride]
    RestoreSensor,
    SensorEntity,
    GeneratorDiagnosticSensorEntity[DiagnosticValue],
):
    """Class representing a sensor for a specific Diagnostic Trouble Code."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def is_available(self) -> bool:
        """Returns true when lamp status is available."""
        return (
            self._restored_data is not None
            or (self._value.is_available and self._value.occurences >= 0)
        ) and super().is_available()

    def update(self) -> None:
        """Set the current value."""
        if self._restored_data:
            self._attr_native_value = self._restored_data.native_value

            self._attr_extra_state_attributes = {"restored": True}
            return

        self._attr_extra_state_attributes = {}

        if self._value.occurences >= 0:
            self._attr_native_value = self._value.occurences
        else:
            self._attr_native_value = "Unknown"


class GeneratorDiagnosticTroubleCodeAlarmSensorEntity(  # pyright: ignore[reportIncompatibleVariableOverride]
    RestoreBinarySensor,
    BinarySensorEntity,
    GeneratorDiagnosticSensorEntity[DiagnosticValue],
):
    """Class representing a sensor for a specific Diagnostic Trouble Code."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def is_available(self) -> bool:
        """Returns true when lamp status is available."""
        return (
            self._restored_data is not None or self._value.is_available
        ) and super().is_available()

    def update(self) -> None:
        """Set the current value."""
        if self._restored_data:
            self.is_on = self._restored_data.native_value == "on"
            self._attr_extra_state_attributes = {"restored": True}
        else:
            self.is_on = self._value.present is True
            self._attr_extra_state_attributes = {}


DTC_SENSORS: dict[str, type[GeneratorDiagnosticSensorEntity[DiagnosticValue]]] = {
    "alarm": GeneratorDiagnosticTroubleCodeAlarmSensorEntity,
    "alarm_fault": GeneratorDiagnosticTroubleCodeFailureMoodeSensorEntity,
    "alarm_occurences": GeneratorDiagnosticTroubleCodeOccurencesSensorEntity,
}


class GeneratorLampAlarmSensorEntity(  # pyright: ignore[reportIncompatibleVariableOverride]
    RestoreBinarySensor,
    BinarySensorEntity,
    GeneratorDiagnosticSensorEntity[LampFlashStatus],
):
    """Class representing a sensor for a specific Diagnostic Trouble Code."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def is_available(self) -> bool:
        """Returns true when lamp status is available."""
        return (
            self._restored_data is not None or self._value.is_available
        ) and super().is_available()

    def update(self) -> None:
        """Set the current value."""
        if self._restored_data:
            self.is_on = self._restored_data.native_value == "on"
            self._attr_extra_state_attributes = {"restored": True}
        else:
            self.is_on = self._value.is_active
            self._attr_extra_state_attributes = {}


class GeneratorLampFlashStatusSensorEntity(  # pyright: ignore[reportIncompatibleVariableOverride]
    RestoreSensor,
    SensorEntity,
    GeneratorDiagnosticSensorEntity[LampFlashStatus],
):
    """Class representing a sensor for a Lamp Status."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
        diagnostic_name: str,
        value_name: str,
        value: LampFlashStatus,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__(gen_data, unique_id, diagnostic_name, value_name, value)

        self.entity_description = SensorEntityDescription(
            key=self._unique_id,
            device_class=SensorDeviceClass.ENUM,
            options=[v.value for v in BlinkDisplayValue],
        )

    def is_available(self) -> bool:
        """Returns true when lamp status is available."""
        return (
            (
                self._restored_data is not None
                and self._restored_data.native_value
                in {value.value for value in BlinkDisplayValue}
            )
            or self._value.is_available
        ) and super().is_available()

    def update(self) -> None:
        """Set the current value."""
        if self._restored_data:
            self._attr_native_value = self._restored_data.native_value
            self._attr_extra_state_attributes = {"restored": True}
        else:
            self._attr_native_value = self._value.blink.value
            self._attr_extra_state_attributes = {}


LAMP_SENSORS: dict[str, type[GeneratorDiagnosticSensorEntity[LampFlashStatus]]] = {
    "alarm": GeneratorLampAlarmSensorEntity,
    "blink_status": GeneratorLampFlashStatusSensorEntity,
}


class GeneratorClientStatusSensorEntity(BinarySensorEntity):
    """Sensor for displaying whether or not the client is connected."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Client Connected"

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__()
        self._attr_unique_id = self._unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{gen_data.client.host}:{gen_data.client.port}")},
        )

        self._gen_data = gen_data

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()
        self._gen_data.client.register_event_callback(lambda _: self.set_connected())
        self.set_connected()

    def set_connected(self) -> None:
        """Set the current value and update the state."""
        self.is_on = self._gen_data.client.is_connected()
        self.schedule_update_ha_state()


class GeneratorClientLastUpdateEntity(RestoreSensor, SensorEntity):
    """Sensor for displaying the last time data was received from the canbus."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Last Update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self,
        gen_data: GeneratorData,
        unique_id: str,
    ) -> None:
        """Initialize a generator sensor."""
        super().__init__()
        self._attr_unique_id = self._unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{gen_data.client.host}:{gen_data.client.port}")},
        )

        self._restored_data = None
        self._gen_data = gen_data
        self._debouncer = None
        self._attr_native_value = None
        self._last_update_time = None

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added."""
        await super().async_added_to_hass()

        self._restored_data = await self.async_get_last_sensor_data()
        if self._restored_data is not None:
            if self._restored_data.native_value is not None:
                _LOGGER.debug("Restored data: %s", self._restored_data.native_value)
                self._attr_native_value = self._restored_data.native_value
                self.schedule_update_ha_state()

        self._gen_data.database.register_event_callback(self._handle_client_event)

    def _handle_client_event(self, event: GeneratorDatabaseEvent) -> None:
        """Handle client events."""
        if not self._debouncer:
            self._debouncer = Debouncer(
                self.hass,
                _LOGGER,
                cooldown=30.0,
                immediate=False,
                function=self.update,
            )
        self._debouncer.async_schedule_call()
        self._last_update_time = datetime.now(tz=UTC)

    def update(self) -> None:
        """Set the current value and update the state."""
        self._attr_native_value = self._last_update_time
        self.schedule_update_ha_state()
