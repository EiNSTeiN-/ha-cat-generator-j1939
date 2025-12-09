"""DM1 message parsing for J1939 protocol."""

from dataclasses import dataclass
from enum import Enum
import logging

from decoda import SPN
from decoda.spec_loader import J1939Spec

_LOGGER = logging.getLogger(__name__)


class LampStatus(Enum):
    """Status of a lamp in a DM1 message."""

    OFF = 0
    ON = 1
    NOT_AVAILABLE_1 = 2
    NOT_AVAILABLE_2 = 3


class FlashStatus(Enum):
    """Status of a lamp in a DM1 message."""

    SLOW = 0
    FAST = 1
    RESERVED = 2
    NO_FLASH = 3


class BlinkDisplayValue(Enum):
    """Display value for lamp blinking status."""

    UNAVAILABLE = "Unavailable"
    UNKNOWN = "Unknown"
    OFF = "Off"
    NOT_BLINKING = "Not blinking"
    BLINKING_SLOW = "Blinking (slow)"
    BLINKING_FAST = "Blinking (fast)"


@dataclass
class LampFlashStatus:
    """Flash status of a lamp in a DM1 message."""

    name: str
    status: LampStatus
    flash: FlashStatus

    @property
    def is_unavailable(self) -> bool:
        """Return true if the lamp status is unavailable."""
        return self.status in (LampStatus.NOT_AVAILABLE_1, LampStatus.NOT_AVAILABLE_2)

    @property
    def is_available(self) -> bool:
        """Return true if the lamp status is unavailable."""
        return not self.is_unavailable

    @property
    def is_active(self) -> bool:
        """Return true if the lamp is active, regardless of blinking speed."""
        return self.status == LampStatus.ON

    @property
    def blink(self) -> BlinkDisplayValue:
        """Return a string representation of the lamp state."""
        if self.is_unavailable:
            return BlinkDisplayValue.UNAVAILABLE

        if self.status == LampStatus.OFF:
            return BlinkDisplayValue.OFF

        if self.flash == FlashStatus.NO_FLASH:
            return BlinkDisplayValue.NOT_BLINKING

        if self.flash == FlashStatus.SLOW:
            return BlinkDisplayValue.BLINKING_SLOW

        if self.flash == FlashStatus.FAST:
            return BlinkDisplayValue.BLINKING_FAST

        return BlinkDisplayValue.UNKNOWN

    def __repr__(self) -> str:
        """Return a string representation of the lamp flash status."""
        return f"LampFlashStatus(name={self.name}, status={self.status}, flash={self.flash})"

    def __eq__(self, other: object) -> bool:
        """Check equality with another LampFlashStatus."""
        if not isinstance(other, LampFlashStatus):
            return NotImplemented
        return (
            self.name == other.name
            and self.status == other.status
            and self.flash == other.flash
        )


@dataclass
class LampState:
    """State of the various lamps in a DM1 message."""

    protect: LampFlashStatus
    amber_warning: LampFlashStatus
    red_stop: LampFlashStatus
    malfunction: LampFlashStatus

    def __repr__(self) -> str:
        """Return a string representation of the lamp state."""
        return (
            f"LampState(protect={self.protect.blink.value}, amber_warning={self.amber_warning.blink.value}, "
            f"red_stop={self.red_stop.blink.value}, malfunction={self.malfunction.blink.value})"
        )

    def __eq__(self, other: object) -> bool:
        """Check equality with another LampState."""
        if not isinstance(other, LampState):
            return NotImplemented
        return (
            self.protect == other.protect
            and self.amber_warning == other.amber_warning
            and self.red_stop == other.red_stop
            and self.malfunction == other.malfunction
        )


@dataclass
class DTC:
    """Diagnostic Trouble Code (DTC)."""

    spn: SPN
    fmi: int
    oc: int
    cm: int  # Conversion Method (0 = 19-bit SPN, 1 = 11-bit SPN)

    def __repr__(self) -> str:
        """Return a string representation of the DTC."""
        return f"DTC(spn={self.spn!r}, fmi={self.fmi}, oc={self.oc}, cm={self.cm})"


@dataclass
class DM1Message:
    """DM1 message containing lamp states and DTCs."""

    lamps: LampState
    dtcs: list[DTC]

    def __repr__(self) -> str:
        """Return a string representation of the DM1 message."""
        return f"DM1Message(lamps={self.lamps!r}, dtcs={self.dtcs!r})"


def parse_lamp_status(status: int, flash: int) -> LampState:
    """Parse the lamp status from the first byte.

    See https://www.csselectronics.com/pages/j1939-73-dm1-diagnostic-message-dtc
    """
    malfunction = LampFlashStatus(
        "malfunction",
        LampStatus((status >> 6) & 0b11),
        FlashStatus((flash >> 6) & 0b11),
    )
    red = LampFlashStatus(
        "red_stop", LampStatus((status >> 4) & 0b11), FlashStatus((flash >> 4) & 0b11)
    )
    amber = LampFlashStatus(
        "amber_warning",
        LampStatus((status >> 2) & 0b11),
        FlashStatus((flash >> 2) & 0b11),
    )
    protect = LampFlashStatus(
        "protect", LampStatus(status & 0b11), FlashStatus(flash & 0b11)
    )
    return LampState(
        protect=protect,
        amber_warning=amber,
        red_stop=red,
        malfunction=malfunction,
    )


def parse_dtc(spec: J1939Spec, bytes_: bytes) -> DTC | None:
    """Parse a DTC from 4 bytes. Returns None if bytes are invalid."""
    if len(bytes_) < 4:
        return None

    b1, b2, b3, b4 = bytes_

    # 19-bit SPN across b1, b2, and lower 3 bits of b3
    spn_id = b1 | (b2 << 8) | ((b3 & 0xE0) << 11)

    fmi = b3 & 0x1F  # lower 5 bits
    cm = (b3 >> 7) & 0x01  # bit 7
    oc = b4

    _LOGGER.debug("Parsed DTC: spn_id=%d, fmi=%d, cm=%d, oc=%d", spn_id, fmi, cm, oc)

    if spn_id == 0:
        return None

    return DTC(spn=spec.SPNs.get_by_id(spn_id), fmi=fmi, oc=oc, cm=cm)


def parse(spec: J1939Spec, data: bytes) -> DM1Message:
    """Parse a DM1 message from raw bytes."""
    if len(data) < 2:
        raise ValueError("DM1 message too short")

    lamp_status = parse_lamp_status(data[0], data[1])

    dtcs: list[DTC] = []
    for i in range(2, len(data), 4):
        block = data[i : i + 4]
        if len(block) < 4 or all(b == 0xFF for b in block):
            break
        dtc = parse_dtc(spec, block)
        if dtc:
            dtcs.append(dtc)

    return DM1Message(lamps=lamp_status, dtcs=dtcs)
