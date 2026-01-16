#!/usr/bin/env python3
import asyncio
import sys
import telnetlib3
import socket
import logging
import argparse
import signal
import inspect

# Python 3.6 compatibility
if sys.version_info >= (3, 7):
    create_task = asyncio.create_task
else:
    create_task = asyncio.ensure_future

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
log = logging.getLogger(__name__)


class RemoteSession:
    """Manages connection to the remote telnet server with reconnection logic."""

    IAC = b"\xff"  # Interpret As Command
    NOP = b"\xf1"  # No Operation (used as keepalive)

    def __init__(self, remote_server, remote_port, heartbeat=30, reconnect_delay=5, queue_limit=1000):
        self.remote_server = remote_server
        self.remote_port = remote_port
        self.reader = None
        self.writer = None
        self.heartbeat = heartbeat
        self.reconnect_delay = reconnect_delay
        self.queue = asyncio.Queue(maxsize=queue_limit)
        self.closing = False
        self.close_event = asyncio.Event()
        self.flush_task = None

    async def connect(self):
        """Establish connection to remote server with retry logic."""
        while not self.closing:
            try:
                log.info(f"Connecting to remote {self.remote_server}:{self.remote_port}")
                self.reader, self.writer = await asyncio.wait_for(
                    telnetlib3.open_connection(
                        host=self.remote_server,
                        port=self.remote_port
                    ),
                    timeout=10
                )
                sock = self.writer.get_extra_info("socket")
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log.info("Connected to remote Telnet server")
                return True
            except (OSError, asyncio.TimeoutError) as e:
                log.warning(f"Remote connection failed ({e})")
                await asyncio.sleep(self.reconnect_delay)
        return False

    async def _reader_at_eof(self):
        if not self.reader:
            return True
        try:
            at_eof = self.reader.at_eof()
        except Exception:
            return False
        if inspect.isawaitable(at_eof):
            return await at_eof
        return bool(at_eof)

    async def read_loop(self, callback):
        """Read from remote and send to all clients via callback."""
        # Start the flush queue task once
        if not self.flush_task or self.flush_task.done():
            self.flush_task = create_task(self.flush_queue_loop())

        while not self.closing:
            if not self.reader or not self.writer:
                if not await self.connect():
                    break

            try:
                read_task = create_task(self.reader.read(1024))
                close_task = create_task(self.close_event.wait())
                done, pending = await asyncio.wait(
                    {read_task, close_task},
                    timeout=self.heartbeat,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if close_task in done:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    break
                if not done:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    await self.send_heartbeat()
                    continue
                if read_task in done:
                    try:
                        data = read_task.result()
                    except asyncio.TimeoutError:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        await self.send_heartbeat()
                        continue
                    except Exception as e:
                        log.warning(f"Remote read error: {e}")
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        await self.cleanup_connection()
                        continue
                    if not data or await self._reader_at_eof():
                        log.warning("Remote connection lost - reconnecting...")
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        await self.cleanup_connection()
                        continue
                    await callback(data)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

            except asyncio.TimeoutError:
                # No data received within heartbeat interval - probe connection
                await self.send_heartbeat()
            except asyncio.CancelledError:
                log.info("Read loop cancelled")
                break
            except Exception as e:
                log.warning(f"Remote read error: {e}")
                await self.cleanup_connection()

    async def send_heartbeat(self):
        """Send IAC NOP as keepalive to probe connection health."""
        if not self.writer:
            return
        try:
            self.writer.write(self.IAC + self.NOP)
            await asyncio.wait_for(self.writer.drain(), timeout=10)
            log.debug("Sent remote heartbeat (IAC NOP)")
        except Exception as e:
            log.warning(f"Remote heartbeat failed: {e}")
            await self.cleanup_connection()

    async def write(self, data):
        """Queue data to be sent to remote."""
        if self.closing:
            return
        try:
            await asyncio.wait_for(self.queue.put(data), timeout=1)
        except asyncio.TimeoutError:
            log.warning("Remote send queue full - dropping data")
        except Exception as e:
            log.warning(f"Queue error: {e}")

    async def flush_queue_loop(self):
        """Process queued data and send to remote."""
        while not self.closing:
            try:
                data = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue

            if not data:
                continue

            # Wait for connection
            retry_count = 0
            while not self.writer and not self.closing and retry_count < 10:
                await asyncio.sleep(0.5)
                retry_count += 1

            if not self.writer:
                log.warning("No remote connection - dropping queued data")
                continue

            try:
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), timeout=5)
                log.debug(f"Sent {len(data)} bytes to remote")
            except Exception as e:
                log.warning(f"Write to remote failed: {e}")
                await self.cleanup_connection()

    async def cleanup_connection(self):
        """Close current connection without setting closing flag."""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                log.debug(f"Error closing remote writer: {e}")
        self.reader = None
        self.writer = None

    async def close(self):
        """Shutdown the remote session completely."""
        if not self.closing:
            self.closing = True
        self.close_event.set()

        # Cancel flush task
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
            try:
                await self.flush_task
            except asyncio.CancelledError:
                pass

        # Close connection
        await self.cleanup_connection()

        # Clear queue
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        log.info("Remote session closed")


class TelnetManager:
    """Manages multiple client connections with broadcasting."""

    IAC = b"\xff"  # Interpret As Command
    NOP = b"\xf1"  # No Operation (used as keepalive)

    def __init__(self, remote, lock, heartbeat=30, client_timeout=86400):
        self.clients = {}
        self.remote = remote
        self.lock = lock
        self.heartbeat = heartbeat
        self.client_timeout = client_timeout

    def register(self, writer):
        """Register a new client connection."""
        peer = writer.get_extra_info('peername')
        self.clients[id(writer)] = writer
        log.info(f"Client connected: {peer}. Total clients: {len(self.clients)}")

    def unregister(self, writer):
        """Unregister a client connection."""
        peer = writer.get_extra_info('peername')
        writer_id = id(writer)

        try:
            if not writer.is_closing():
                writer.close()
        except Exception as e:
            log.debug(f"Error closing writer: {e}")

        self.clients.pop(writer_id, None)
        log.info(f"Client disconnected: {peer}. Remaining: {len(self.clients)}")

    async def broadcast(self, data):
        """Send data to all connected clients."""
        if not data or not self.clients:
            return

        # Work with a snapshot to avoid dict mutation issues
        clients_snapshot = list(self.clients.items())

        for writer_id, writer in clients_snapshot:
            try:
                if writer.is_closing():
                    self.clients.pop(writer_id, None)
                    continue

                writer.write(data)
                await asyncio.wait_for(writer.drain(), timeout=2)
            except Exception as e:
                log.warning(f"Lost client {writer.get_extra_info('peername')}: {e}")
                self.unregister(writer)

    async def client_handler(self, reader, writer):
        """Handle individual client connection."""
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.register(writer)

        async def from_client():
            """Read data from client and forward to remote."""
            try:
                while not writer.is_closing():
                    data = await asyncio.wait_for(reader.read(1024), timeout=self.client_timeout)
                    if not data:
                        break
                    async with self.lock:
                        await self.remote.write(data)
            except asyncio.TimeoutError:
                log.debug(f"Client {peer} read timeout")
            except asyncio.CancelledError:
                log.debug(f"Client {peer} read cancelled")
            except Exception as e:
                log.warning(f"Client read error {peer}: {e}")

        async def keepalive():
            """Send periodic IAC NOP to keep client connection alive."""
            try:
                while not writer.is_closing():
                    await asyncio.sleep(self.heartbeat)
                    if writer.is_closing():
                        break
                    writer.write(self.IAC + self.NOP)
                    await asyncio.wait_for(writer.drain(), timeout=5)
                    log.debug(f"Sent keepalive to client {peer}")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug(f"Client keepalive failed {peer}: {e}")

        try:
            await asyncio.gather(from_client(), keepalive())
        except Exception as e:
            log.error(f"Client handler error for {peer}: {e}")
        finally:
            log.info(f"Closing client connection {peer}")
            self.unregister(writer)
            try:
                await writer.wait_closed()
            except Exception:
                pass


class ConnectionMuxer:
    """Main multiplexer coordinating remote and client connections."""

    def __init__(self, listen_ip, listen_port, remote_server, remote_port, heartbeat=30, client_timeout=86400):
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.remote = RemoteSession(remote_server, remote_port, heartbeat=heartbeat)
        self.lock = asyncio.Lock()
        self.clients = TelnetManager(self.remote, self.lock, heartbeat=heartbeat, client_timeout=client_timeout)
        self.server = None
        self.read_task = None

    async def start(self):
        """Start the multiplexer service."""
        # Start remote read loop
        self.read_task = create_task(
            self.remote.read_loop(self.clients.broadcast)
        )

        # Start telnet server for clients
        self.server = await telnetlib3.create_server(
            host=self.listen_ip,
            port=self.listen_port,
            shell=self.clients.client_handler
        )
        log.info(f"Telnet proxy listening on {self.listen_ip}:{self.listen_port}")

        try:
            await self.server.wait_closed()
        except asyncio.CancelledError:
            log.info("Server task cancelled")

    async def stop(self):
        """Stop the multiplexer and clean up resources."""
        log.info("Stopping multiplexer...")

        # Cancel read task
        if self.read_task and not self.read_task.done():
            self.read_task.cancel()
            try:
                await self.read_task
            except asyncio.CancelledError:
                pass

        # Close remote session
        await self.remote.close()

        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        # Disconnect all clients
        for writer_id, writer in list(self.clients.clients.items()):
            self.clients.unregister(writer)

        log.info("Multiplexer stopped")


async def main():
    """Main entry point."""
    logging.basicConfig(
        filename='telnet_multiplexer.log',
        level=logging.INFO,
        format=LOG_FORMAT
    )

    parser = argparse.ArgumentParser(description="Telnet Proxy Multiplexer")
    parser.add_argument("--remote-server", default="127.0.0.1",
                       help="Remote server IP or hostname")
    parser.add_argument("--remote-port", type=int, default=5100,
                       help="Remote server port")
    parser.add_argument("--listen-ip", default="0.0.0.0",
                       help="IP address to listen on")
    parser.add_argument("--listen-port", type=int, default=5000,
                       help="Port to listen on")
    parser.add_argument("--heartbeat", type=int, default=30,
                       help="Heartbeat interval in seconds (default: 30)")
    parser.add_argument("--client-timeout", type=int, default=86400,
                       help="Client read timeout in seconds (default: 24 hours)")
    args = parser.parse_args()

    multiplexer = ConnectionMuxer(
        listen_ip=args.listen_ip,
        listen_port=args.listen_port,
        remote_server=args.remote_server,
        remote_port=args.remote_port,
        heartbeat=args.heartbeat,
        client_timeout=args.client_timeout,
    )

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        # Start multiplexer
        start_task = create_task(multiplexer.start())

        # Wait for shutdown signal
        await shutdown_event.wait()

        log.info("Shutting down...")
        await multiplexer.stop()

        # Cancel start task if still running
        if not start_task.done():
            start_task.cancel()
            try:
                await start_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        log.error(f"Multiplexer failure: {e}")
        await multiplexer.stop()


if __name__ == "__main__":
    asyncio.run(main())
