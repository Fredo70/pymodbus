"""Modbus client async TCP communication."""
import asyncio
import select
import socket
import time
from typing import Any, Tuple, Type

from pymodbus.client.base import ModbusBaseClient
from pymodbus.exceptions import ConnectionException
from pymodbus.framer import ModbusFramer
from pymodbus.framer.socket_framer import ModbusSocketFramer
from pymodbus.logging import Log
from pymodbus.utilities import ModbusTransactionState


class AsyncModbusTcpClient(ModbusBaseClient, asyncio.Protocol):
    """**AsyncModbusTcpClient**.

    :param host: Host IP address or host name
    :param port: (optional) Port used for communication
    :param framer: (optional) Framer class
    :param source_address: (optional) source address of client
    :param kwargs: (optional) Experimental parameters

    using unix domain socket can be achieved by setting host="unix:<path>"

    Example::

        from pymodbus.client import AsyncModbusTcpClient

        async def run():
            client = AsyncModbusTcpClient("localhost")

            await client.connect()
            ...
            client.close()
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        framer: Type[ModbusFramer] = ModbusSocketFramer,
        source_address: Tuple[str, int] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize Asyncio Modbus TCP Client."""
        asyncio.Protocol.__init__(self)
        ModbusBaseClient.__init__(self, framer=framer, **kwargs)
        self.params.host = host
        self.params.port = port
        self.params.source_address = source_address
        if "internal_no_setup" in kwargs:
            return
        if host.startswith("unix:"):
            self.new_transport.setup_unix(False, host[5:])
        else:
            self.new_transport.setup_tcp(False, host, port)

    async def connect(self) -> bool:
        """Initiate connection to start client."""

        # if reconnect_delay_current was set to 0 by close(), we need to set it back again
        # so this instance will work
        self.new_transport.reset_delay()

        # force reconnect if required:
        Log.debug(
            "Connecting to {}:{}.",
            self.new_transport.comm_params.host,
            self.new_transport.comm_params.port,
        )
        return await self.new_transport.transport_connect()

    @property
    def connected(self):
        """Return true if connected."""
        return self.new_transport.is_active()


class ModbusTcpClient(ModbusBaseClient):
    """**ModbusTcpClient**.

    :param host: Host IP address or host name
    :param port: (optional) Port used for communication
    :param framer: (optional) Framer class
    :param source_address: (optional) source address of client
    :param kwargs: (optional) Experimental parameters

    using unix domain socket can be achieved by setting host="unix:<path>"

    Example::

        from pymodbus.client import ModbusTcpClient

        async def run():
            client = ModbusTcpClient("localhost")

            client.connect()
            ...
            client.close()

    Remark: There are no automatic reconnect as with AsyncModbusTcpClient
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        framer: Type[ModbusFramer] = ModbusSocketFramer,
        source_address: Tuple[str, int] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize Modbus TCP Client."""
        super().__init__(framer=framer, **kwargs)
        self.params.host = host
        self.params.port = port
        self.params.source_address = source_address
        self.socket = None
        self.use_sync = True

    @property
    def connected(self):
        """Connect internal."""
        return self.socket is not None

    def connect(self):  # pylint: disable=invalid-overridden-method
        """Connect to the modbus tcp server."""
        if self.socket:
            return True
        try:
            if self.params.host.startswith("unix:"):
                self.socket = socket.socket(socket.AF_UNIX)
                self.socket.settimeout(self.params.timeout)
                self.socket.connect(self.params.host[5:])
            else:
                self.socket = socket.create_connection(
                    (self.params.host, self.params.port),
                    timeout=self.params.timeout,
                    source_address=self.params.source_address,
                )
            Log.debug(
                "Connection to Modbus server established. Socket {}",
                self.socket.getsockname(),
            )
        except OSError as msg:
            Log.error(
                "Connection to ({}, {}) failed: {}",
                self.params.host,
                self.params.port,
                msg,
            )
            self.close()
        return self.socket is not None

    def close(self):  # pylint: disable=arguments-differ
        """Close the underlying socket connection."""
        if self.socket:
            self.socket.close()
        self.socket = None

    def _check_read_buffer(self):
        """Check read buffer."""
        time_ = time.time()
        end = time_ + self.params.timeout
        data = None
        ready = select.select([self.socket], [], [], end - time_)
        if ready[0]:
            data = self.socket.recv(1024)
        return data

    def send(self, request):
        """Send data on the underlying socket."""
        super().send(request)
        if not self.socket:
            raise ConnectionException(str(self))
        if self.state == ModbusTransactionState.RETRYING:
            if data := self._check_read_buffer():
                return data

        if request:
            return self.socket.send(request)
        return 0

    def recv(self, size):
        """Read data from the underlying descriptor."""
        super().recv(size)
        if not self.socket:
            raise ConnectionException(str(self))

        # socket.recv(size) waits until it gets some data from the host but
        # not necessarily the entire response that can be fragmented in
        # many packets.
        # To avoid split responses to be recognized as invalid
        # messages and to be discarded, loops socket.recv until full data
        # is received or timeout is expired.
        # If timeout expires returns the read data, also if its length is
        # less than the expected size.
        self.socket.setblocking(0)

        timeout = self.params.timeout

        # If size isn't specified read up to 4096 bytes at a time.
        if size is None:
            recv_size = 4096
        else:
            recv_size = size

        data = []
        data_length = 0
        time_ = time.time()
        end = time_ + timeout
        while recv_size > 0:
            try:
                ready = select.select([self.socket], [], [], end - time_)
            except ValueError:
                return self._handle_abrupt_socket_close(size, data, time.time() - time_)
            if ready[0]:
                if (recv_data := self.socket.recv(recv_size)) == b"":
                    return self._handle_abrupt_socket_close(
                        size, data, time.time() - time_
                    )
                data.append(recv_data)
                data_length += len(recv_data)
            time_ = time.time()

            # If size isn't specified continue to read until timeout expires.
            if size:
                recv_size = size - data_length

            # Timeout is reduced also if some data has been received in order
            # to avoid infinite loops when there isn't an expected response
            # size and the slave sends noisy data continuously.
            if time_ > end:
                break

        return b"".join(data)

    def _handle_abrupt_socket_close(self, size, data, duration):
        """Handle unexpected socket close by remote end.

        Intended to be invoked after determining that the remote end
        has unexpectedly closed the connection, to clean up and handle
        the situation appropriately.

        :param size: The number of bytes that was attempted to read
        :param data: The actual data returned
        :param duration: Duration from the read was first attempted
               until it was determined that the remote closed the
               socket
        :return: The more than zero bytes read from the remote end
        :raises ConnectionException: If the remote end didn't send any
                 data at all before closing the connection.
        """
        self.close()
        size_txt = size if size else "unbounded read"
        readsize = f"read of {size_txt} bytes"
        msg = (
            f"{self}: Connection unexpectedly closed "
            f"{duration} seconds into {readsize}"
        )
        if data:
            result = b"".join(data)
            Log.warning(" after returning {} bytes: {} ", len(result), result)
            return result
        msg += " without response from slave before it closed connection"
        raise ConnectionException(msg)

    def is_socket_open(self):
        """Check if socket is open."""
        return self.socket is not None

    def __str__(self):
        """Build a string representation of the connection.

        :returns: The string representation
        """
        return f"ModbusTcpClient({self.params.host}:{self.params.port})"

    def __repr__(self):
        """Return string representation."""
        return (
            f"<{self.__class__.__name__} at {hex(id(self))} socket={self.socket}, "
            f"ipaddr={self.params.host}, port={self.params.port}, timeout={self.params.timeout}>"
        )
