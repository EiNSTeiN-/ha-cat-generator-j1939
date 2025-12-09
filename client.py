"""Client for connecting to a CAT generator over TCP/CAN and decoding J1939 messages."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time

import can
from decoda import ConnectionManager, Decoda
from decoda.main import PGN, DecodedSPN, UnknownPGN
from decoda.spec_loader import J1939Spec, load_from_file
from decoda.transport import Message as DecodaMessage

from . import dm1
from .const import DECODA_SPEC_PATH
from .database import GeneratorDatabase
from .tcp_can_bus import TcpCanBus

_LOGGER = logging.getLogger(__name__)

ENGINE_CONTROLLER_ADDRESS = 0


class MessageHandler(ABC):
    """Handles incoming CAN messages and decodes them."""

    def __init__(self, database: GeneratorDatabase) -> None:
        """Initialize the message handler."""
        self.spec: J1939Spec = load_from_file(DECODA_SPEC_PATH)
        self.decoda = Decoda(self.spec, self.decoded_message_handler)
        self.cm = ConnectionManager(self.decoda, self.defrag_error_handler)
        self.database = database

    def decoded_message_handler(self, m: DecodaMessage):
        """Handle a decoded message."""
        if isinstance(m.pgn, UnknownPGN):
            if m.pgn.id == 59904:
                # PGN 59904 is a request PGN, ignore it
                return

            if m.pgn.id == 65226:
                # PGN 59904 is a request PGN, ignore it
                decoded = dm1.parse(self.spec, m.pgn.payload)
                _LOGGER.warning(
                    f"p={m.priority!r}  src={m.src_address!r}  dst={m.dst_address!r}  pgn={m.pgn.id!r}  {decoded!r}"
                )

                self.database.set_lamps(decoded.lamps)
                self.database.set_diagnostics(decoded.dtcs)

                return

            _LOGGER.warning(
                f"p={m.priority!r}  src={m.src_address!r}  dst={m.dst_address!r}  pgn={m.pgn.id!r}  Unknown PGN"
            )
            return

        spn: DecodedSPN
        for spn in m.decoded:
            spn_def = self.spec.SPNs.get_by_id(spn.id)
            self.database.set(m.pgn, spn_def, spn)

    def defrag_error_handler(self, reason, info):
        """Handle defragmentation errors."""
        _LOGGER.error(f"Handle the error if we care: {reason} - {info}")

    def handle_frame(self, msg: can.Message):
        """Handle a raw CAN message frame by passing it to Decoda."""
        try:
            _LOGGER.debug(
                f"RECV id=0x{msg.arbitration_id:X} dlc={msg.dlc} data={msg.data.hex()}"
            )
            self.decoda.handle_frame(msg.arbitration_id, msg.data)
        except Exception as e:
            _LOGGER.error(f"{type(e).__name__} Error handling frame: {e}")
            raise

    @abstractmethod
    def loop(self):
        """Main receive loop to be run in a thread."""

    @abstractmethod
    def request(self, sa: int, da: int, pgn_id: int):
        """Make a PGN request."""


@dataclass
class GeneratorClientEvent:
    """Client event."""


@dataclass
class GeneratorClientConnectedEvent(GeneratorClientEvent):
    """The client is connected."""


@dataclass
class GeneratorClientDisconnectedEvent(GeneratorClientEvent):
    """The client is disconnected."""


class GeneratorClient(MessageHandler):
    """Client for connecting to a CAT generator over TCP/CAN."""

    def __init__(
        self,
        database: GeneratorDatabase,
        host: str,
        port: int,
        wait_reconnect: int = 10,
        address: int = 0,
    ) -> None:
        """Initialize the generator client with a host and port."""
        super().__init__(database=database)
        self.hass = database.hass
        self.host = host
        self.port = port
        self.wait_reconnect = wait_reconnect
        self.address = address
        self._bus: TcpCanBus | None = None
        self._lock = threading.Lock()
        self._queue: list[can.Message] = []
        self._queue_lock = threading.Lock()
        self._callbacks: list[Callable[[GeneratorClientEvent], None]] = []
        self._is_connected = None

    def register_event_callback(
        self, callback: Callable[[GeneratorClientEvent], None]
    ) -> None:
        """Register a callback to be called when the database is updated."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def trigger_event(self, event: GeneratorClientEvent) -> None:
        """Trigger an event."""
        for callback in self._callbacks:
            self.hass.loop.call_soon_threadsafe(callback, event)

    def loop(self):
        """Main loop to connect and process CAN messages."""
        while True:
            try:
                self._bus = TcpCanBus(self.host, self.port)
            except ConnectionRefusedError as e:
                _LOGGER.error("Connection refused: %s", e)
                self.disconnected()
                time.sleep(self.wait_reconnect)
                continue

            try:
                with self._bus:
                    _LOGGER.info(
                        "Connected to CAT generator at %s:%i", self.host, self.port
                    )
                    self._is_connected = True
                    self.trigger_event(GeneratorClientConnectedEvent())
                    while True:
                        with self._lock:
                            msg = self._bus.recv(0.1)
                            if not msg:
                                with self._queue_lock:
                                    if len(self._queue) > 0:
                                        msg = self._queue.pop(0)
                                        _LOGGER.debug("Sending %r", msg)
                                        self._bus.send(msg)
                                continue
                            self.handle_frame(msg)

            except (ConnectionResetError, can.CanError) as e:
                _LOGGER.error("Connection reset: %s", e)
                self.disconnected()

    def disconnected(self):
        """Handle disconnection."""
        if self._bus:
            self._bus.shutdown()
            self._bus = None
        self._is_connected = False
        self.trigger_event(GeneratorClientDisconnectedEvent())

    def is_connected(self) -> bool:
        """Return True if the client is connected."""
        return bool(self._is_connected)

    def build_can_id(
        self,
        priority: int,
        sa: int,
        da: int,
        pgn_id: int,
        edp: bool = False,
        dp: bool = False,
    ) -> int:
        """Compute CAN ID from priority, source address and PGN id."""
        if pgn_id >= 0xF000:
            raise ValueError(f"PGN {pgn_id} is not requestable")

        # Extract fields
        priority = priority & 0x7
        # Compose the 18-bit PGN
        can_pgn = int(edp) << 17 | int(dp) << 16 | (pgn_id & 0xFF00) | (da & 0xFF)
        # Compose the CAN ID
        return (priority << 26) | (can_pgn << 8) | (sa & 0xFF)

    def request(self, sa: int, da: int, pgn_id: int):
        """Send a J1939 message over CAN/TCP."""
        payload = (pgn_id).to_bytes(3, byteorder="little")
        msg = can.Message(
            arbitration_id=self.build_can_id(6, sa, da, 59904),
            dlc=len(payload),
            data=payload,
            is_extended_id=True,
        )
        with self._queue_lock:
            _LOGGER.debug("Queueing %r", msg)
            self._queue.append(msg)

    def request_pgn(self, pgn: PGN, da: int):
        """Request a PGN from a specific destination address."""
        if not isinstance(pgn, PGN):
            raise TypeError(f"Not a PGN: {pgn!r}")

        self.request(self.address, da, pgn.id)
