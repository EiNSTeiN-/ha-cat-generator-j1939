import socket
import struct
import can


class TcpCanBus(can.BusABC):
    """CAN bus interface over a TCP socket."""

    FRAME_FORMAT = "=IB3x8s"
    FRAME_SIZE = struct.calcsize(FRAME_FORMAT)

    def __init__(self, host, port):
        super().__init__("can0")
        self.sock = socket.create_connection((host, port))
        self.sock.settimeout(None)

    def recv(self, timeout=None):
        prev_timeout = self.sock.gettimeout()
        try:
            # Apply the requested timeout
            self.sock.settimeout(timeout)
            data = bytearray()
            # Read until a full frame is available or timeout
            while len(data) < self.FRAME_SIZE:
                # try:
                chunk = self.sock.recv(self.FRAME_SIZE - len(data))
                if chunk == b"":
                    raise can.CanError("Socket connection broken")
                # except OSError as err:
                #     # Underlying socket error
                #     raise can.CanError("Socket error") from err
                if not chunk:
                    # Empty read => peer has closed the connection
                    return
                data.extend(chunk)
        except TimeoutError:
            return None
        finally:
            # Restore original timeout
            self.sock.settimeout(prev_timeout)

        can_id, dlc, raw_data = struct.unpack(self.FRAME_FORMAT, data)
        return can.Message(arbitration_id=can_id, dlc=dlc, data=raw_data[:dlc])

    def send(self, msg, timeout=None):
        frame = struct.pack(
            self.FRAME_FORMAT, msg.arbitration_id, msg.dlc, msg.data.ljust(8, b"\x00")
        )
        self.sock.sendall(frame)

    def shutdown(self):
        self.sock.close()
        super().shutdown()
