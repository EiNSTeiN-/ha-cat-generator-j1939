"""Database for holding current values from the generator."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import threading
from typing import Any

from decoda import PGN, SPN, DecodedSPN, EncodedValue, ScalarValue
from decoda.exceptions import (
    ErrorIndicatorRangeError,
    NotAvaiableRangeError,
    ParameterSpecificIndicatorError,
    ScalarRangeError,
)

from homeassistant.core import HomeAssistant

from .dm1 import DTC, LampState


class Value:
    """Holds a value from a PGN and SPN."""

    def __init__(
        self,
        pgn: PGN,
        spn: SPN,
        value: DecodedSPN | None,
        seen_at: datetime | None = None,
    ) -> None:
        """Initialize the value."""
        self.pgn = pgn
        self.spn = spn
        self._value: DecodedSPN | None = value
        self.seen_at: datetime = datetime.now() if seen_at is None else seen_at

    @property
    def raw(self) -> Any:
        """Return the raw value."""
        return None if self._value is None else self._value.raw

    @property
    def decoded(self) -> Any:
        """Return the value."""
        return None if self._value is None else self._value.value

    @property
    def display(self) -> str | None:
        """Return the display value."""
        return None if self._value is None else self._value.display_value

    @property
    def units(self) -> str | None:
        """Return the display value."""
        return str(self.spn.value_decoder.units) if self.is_scalar else None

    def is_scalar_error(self, klass: type[ValueError]) -> bool:
        """Return true if the scalar value is an error indicator."""
        if not self.is_scalar:
            raise TypeError("Not a scalar SPN")
        if self._value is None:
            return False
        try:
            self.spn.value_decoder.check_in_valid_range(self.raw)
        except klass:
            return True
        except ScalarRangeError:
            # any other scalar range error is not an "error indicator"
            pass
        return False

    def is_encoded_value_error(self) -> bool:
        """Return true if the encoded value is an error indicator."""
        if not self.is_encoded:
            raise TypeError("Not an encoded SPN")
        if self._value is None:
            return False

        if (value := self.decoded) is None:
            # not in the encodings map?
            return False

        if not isinstance(value, str):
            raise TypeError(f"{value!r} Not an encoded string value for {self.spn!r}")

        if value.lower().startswith("error"):
            return True

        return False

    def is_encoded_value_not_available(self) -> bool:
        """Return true if the encoded value is a not available indicator."""
        if not self.is_encoded:
            raise TypeError("Not an encoded SPN")
        if self._value is None:
            return False

        if (value := self.decoded) is None:
            # not in the encodings map?
            return True

        if not isinstance(value, str):
            raise TypeError(f"{value!r} Not an encoded string value for {self.spn!r}")

        if value.lower() in (
            "not available",
            "unavailable",
        ) or value.lower().startswith("don't care"):
            return True

        return False

    @property
    def is_scalar(self) -> bool:
        """Return true if the parameter is a scalar value."""
        return isinstance(self.spn.value_decoder, ScalarValue)

    @property
    def is_encoded(self) -> bool:
        """Return true if the parameter is an encoded value."""
        return isinstance(self.spn.value_decoder, EncodedValue)

    @property
    def is_error_indicator(self) -> bool:
        """Return true if the parameter is an error indicator."""
        if self.is_scalar:
            return self.is_scalar_error(ErrorIndicatorRangeError)
        if self.is_encoded:
            return self.is_encoded_value_error()
        return False

    @property
    def is_not_available(self) -> bool:
        """Return true if the parameter is not available or not requested."""
        if self.is_scalar:
            return self.is_scalar_error(NotAvaiableRangeError)
        if self.is_encoded:
            return self.is_encoded_value_not_available()
        return False

    @property
    def is_param_specific_indicator(self) -> bool:
        """Return true if the parameter is a parameter specific indicator."""
        if self.is_scalar:
            return self.is_scalar_error(ParameterSpecificIndicatorError)
        return False

    @property
    def is_available(self) -> bool:
        """Return true if the parameter is valid (not an error indicator)."""
        if self._value is None:
            return False
        return not (
            self.is_error_indicator
            or self.is_not_available
            or self.is_param_specific_indicator
        )


FMI_VALUES = {
    0: "Data valid but above normal operational range",
    1: "Data valid but below normal operational range",
    2: "Data erratic, intermittent, or incorrect",
    3: "Voltage above normal, or shorted to high source",
    4: "Voltage below normal, or shorted to low source",
    5: "Current below normal or open circuit",
    6: "Current above normal or grounded circuit",
    7: "Mechanical system not responding or out of adjustment",
    8: "Abnormal frequency, pulse width, period, or duty cycle",
    9: "Abnormal update rate",
    10: "Abnormal rate of change",
    11: "Root cause not known",
    12: "Bad intelligent device or component",
    13: "Out of calibration",
    14: "Special instruction #1",
    15: "Data valid but above normal operational range (least severe level)",
    16: "Data valid but above normal operational range (moderatly severe level)",
    17: "Data valid but below normal operational range (least severe level)",
    18: "Data valid but below normal operational range (moderatly severe level)",
    19: "Received network data in error",
    20: "Data drifted high",
    21: "Data drifted low",
    22: "Special instruction #2",
    23: "Request DM60 for additional information",
    24: "Reserved for future use",
    25: "Reserved for future use",
    26: "Reserved for future use",
    27: "Reserved for future use",
    28: "Reserved for future use",
    29: "Reserved for future use",
    30: "Reserved for future use",
    31: "Condition exists",
}


class DiagnosticValue:
    """Holds a value from a PGN and SPN."""

    def __init__(
        self,
        spn: SPN,
        failure_mode_identifier: int,
        occurences: int,
        present: bool | None,
        seen_at: datetime | None = None,
    ) -> None:
        """Initialize the diagnostic value."""
        self.spn = spn
        self.failure_mode_identifier = failure_mode_identifier
        self.occurences = occurences
        self.present = present
        self.seen_at: datetime = datetime.now() if seen_at is None else seen_at

    def __repr__(self) -> str:
        """Return a string representation of the diagnostic value."""
        return (
            f"DiagnosticValue(spn={self.spn.name!r}, "
            f"fmi={self.failure_mode_identifier}, "
            f"occurences={self.occurences}, "
            f"present={self.present})"
        )

    @property
    def is_available(self) -> bool:
        """Return true if the diagnostic value is available."""
        return self.present is not None

    @property
    def display(self) -> str | None:
        """Return the display value."""
        return FMI_VALUES.get(
            self.failure_mode_identifier,
            f"Unknown FMI ({self.failure_mode_identifier})",
        )

    def __eq__(self, other: object) -> bool:
        """Check equality with another DiagnosticValue."""
        if not isinstance(other, DiagnosticValue):
            return NotImplemented
        return (
            self.spn.id == other.spn.id
            and self.failure_mode_identifier == other.failure_mode_identifier
            and self.occurences == other.occurences
            and self.present is other.present
        )


@dataclass
class GeneratorDatabaseEvent[T]:
    """Event for when a value in the database changes."""

    value: T


@dataclass
class GeneratorDatabaseUnchangedEvent[T](GeneratorDatabaseEvent):
    """A value is sent but same as previous value."""


@dataclass
class GeneratorDatabaseUpdatedEvent[T](GeneratorDatabaseEvent):
    """A value is sent and is different from previous value."""

    old_value: T


@dataclass
class GeneratorDatabaseAddedEvent[T](GeneratorDatabaseEvent):
    """A new value is added."""


@dataclass
class GeneratorDatabaseRemovedEvent[T](GeneratorDatabaseEvent):
    """A value is removed."""


class GeneratorDatabase:
    """Holds the current values from the generator."""

    def __init__(self, hass: HomeAssistant, debounce=timedelta(seconds=1)) -> None:
        """Initialize the database."""
        self.hass = hass
        self.data: dict[int, dict[int, Value]] = {}
        self.lamps: LampState | None = None
        self.diagnostics: dict[int, DiagnosticValue] = {}
        self.lock = threading.Lock()
        self._callbacks: list[Callable[[GeneratorDatabaseEvent], None]] = []
        self.debounce = debounce

    def get(self, pgn: PGN, spn: SPN) -> Value | None:
        """Update a value in the database."""
        with self.lock:
            return self.data.get(pgn.id, {}).get(spn.id)

    def set(self, pgn: PGN, spn: SPN, value: DecodedSPN | None) -> Value:
        """Update a value in the database."""
        with self.lock:
            if pgn.id not in self.data:
                self.data[pgn.id] = {}
            if spn.id in self.data[pgn.id]:
                prev = self.data[pgn.id][spn.id]
            else:
                prev = None
            new = Value(pgn, spn, value)

            if prev is not None and new is not None:
                seen_ago = new.seen_at - prev.seen_at
                if seen_ago.total_seconds() < self.debounce.total_seconds():
                    return prev

            self.data[pgn.id][spn.id] = new

            if prev is None:
                event = GeneratorDatabaseAddedEvent[Value](value=new)
            elif prev.raw == new.raw:
                event = GeneratorDatabaseUnchangedEvent[Value](value=new)
            else:
                event = GeneratorDatabaseUpdatedEvent[Value](old_value=prev, value=new)

            for callback in self._callbacks:
                self.hass.loop.call_soon_threadsafe(callback, event)

            return new

    def set_lamps(self, new: LampState) -> None:
        """Update a value in the database."""
        with self.lock:
            prev = self.lamps
            self.lamps = new

            if prev is None:
                event = GeneratorDatabaseAddedEvent[LampState](value=new)
            elif prev == new:
                event = GeneratorDatabaseUnchangedEvent[LampState](value=new)
            else:
                event = GeneratorDatabaseUpdatedEvent[LampState](
                    old_value=prev, value=new
                )

            for callback in self._callbacks:
                self.hass.loop.call_soon_threadsafe(callback, event)

    def get_diagnostics(self, spn: SPN) -> DiagnosticValue | None:
        """Update a value in the database."""
        with self.lock:
            return self.diagnostics.get(spn.id, None)

    def restore_diagnostics(self, diagnostics: list[DiagnosticValue]) -> None:
        """Restore diagnostic values in the database."""
        with self.lock:
            for diagnostic in diagnostics:
                self.diagnostics[diagnostic.spn.id] = diagnostic

                event = GeneratorDatabaseAddedEvent[DiagnosticValue](value=diagnostic)
                for callback in self._callbacks:
                    self.hass.loop.call_soon_threadsafe(callback, event)

    def set_diagnostics(self, diagnostics: list[DTC]) -> None:
        """Update a value in the database."""
        with self.lock:
            current_spn_ids = {dtc.spn.id for dtc in diagnostics}
            for diagnostic in list(self.diagnostics.values()):
                if diagnostic.spn.id in current_spn_ids:
                    continue

                prev = diagnostic
                self.diagnostics[diagnostic.spn.id] = new = DiagnosticValue(
                    spn=diagnostic.spn,
                    failure_mode_identifier=prev.failure_mode_identifier,
                    occurences=prev.occurences,
                    present=False,
                )
                event = GeneratorDatabaseUpdatedEvent[DiagnosticValue](
                    value=new, old_value=prev
                )
                for callback in self._callbacks:
                    self.hass.loop.call_soon_threadsafe(callback, event)

            for diagnostic in diagnostics:
                if diagnostic.spn.id not in self.diagnostics:
                    prev = None
                else:
                    prev = self.diagnostics[diagnostic.spn.id]

                self.diagnostics[diagnostic.spn.id] = new = DiagnosticValue(
                    spn=diagnostic.spn,
                    failure_mode_identifier=diagnostic.fmi,
                    occurences=diagnostic.oc,
                    present=True,
                )

                if prev is None:
                    event = GeneratorDatabaseAddedEvent[DiagnosticValue](value=new)
                elif prev == new:
                    event = GeneratorDatabaseUnchangedEvent[DiagnosticValue](value=new)
                else:
                    event = GeneratorDatabaseUpdatedEvent[DiagnosticValue](
                        old_value=prev, value=new
                    )

                for callback in self._callbacks:
                    self.hass.loop.call_soon_threadsafe(callback, event)

    def register_event_callback(
        self, callback: Callable[[GeneratorDatabaseEvent], None]
    ) -> None:
        """Register a callback to be called when the database is updated."""
        with self.lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
