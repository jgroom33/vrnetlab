#!/usr/bin/env python3
import asyncio
import telnetlib3
import socket
import logging
import argparse

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
logging.basicConfig(
    filename='telnet_multiplexer.log',
    level=logging.INFO,
    format=LOG_FORMAT
)
log = logging.getLogger(__name__)


class RemoteSession:
    IAC = b"\xff"
    NOP = b"\xf1"

    def __init__(self, remote_server, remote_port, heartbeat=30, reconnect_delay=5, queue_limit=1000):
        self.remote_server = remote_server
        self.remote_port = remote_port
        self.reader = None
        self.writer = None
        self.heartbeat = heartbeat
        self.reconnect_delay = reconnect_delay
        self.queue = asyncio.Queue(maxsize=queue_limit)
        self.closing = False

    async def connect(self):
        while not self.closing:
            try:
                log.info(f"Connecting to remote {self.remote_server}:{self.remote_port}")
                self.reader, self.writer = await telnetlib3.open_connection(
                    host=self.remote_server, port=self.remote_port
                )
                sock = self.writer.get_extra_info("socket")
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log.info("Connected to remote Telnet server")
                return
            except OSError as e:
                log.warning(f"Remote connection failed ({e})")
                await asyncio.sleep(self.reconnect_delay)

    async def read_loop(self, callback):
        asyncio.create_task(self.flush_queue_loop())
        while not self.closing:
            if not self.reader:
                await self.connect()

            try:
                data = await asyncio.wait_for(self.reader.read(1024), timeout=self.heartbeat)
                if not data or self.reader.at_eof():
                    log.warning("Reconnecting remote...")
                    await self.close()
                    continue
                await callback(data)

            except asyncio.TimeoutError:
                await self.send_heartbeat()
            except Exception as e:
                log.warning(f"Remote read error: {e}")
                await self.close()

    async def send_heartbeat(self):
        if not self.writer:
            return
        try:
            self.writer.write(self.IAC + self.NOP)
            await asyncio.wait_for(self.writer.drain(), timeout=10)
            log.debug("Sent remote heartbeat (IAC NOP).")
        except Exception as e:
            log.warning(f"Remote heartbeat failed: {e}")
            await self.close()

    async def write(self, data):
        if self.closing:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            log.warning("Remote send queue full")

    async def flush_queue_loop(self):
        while not self.closing:
            data = await self.queue.get()
            if not data:
                continue

            while not self.writer and not self.closing:
                await asyncio.sleep(0.5)

            try:
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), timeout=10)
                log.debug(f"Sent {len(data)} bytes to remote.")
            except Exception as e:
                log.warning(f"Write failed: {e}")
                await self.close()
                try:
                    self.queue.put_nowait(data)
                except asyncio.QueueFull:
                    log.warning("Queue full")

    async def close(self):
        if self.closing:
            return
        self.closing = True
        if self.writer:
            try:
                self.writer.close()
            except Exception as e:
                log.debug(f"Error closing remote writer: {e}")
        self.reader = None
        self.writer = None
        self.closing = False


class TelnetManager:
    IAC = b"\xff"
    NOP = b"\xf1"

    def __init__(self, remote, lock, heartbeat=30):
        self.clients = set()
        self.remote = remote
        self.lock = lock
        self.heartbeat = heartbeat

    def register(self, writer):
        self.clients.add(writer)
        peer = writer.get_extra_info('peername')
        log.info(f"Client connected: {peer}")

    def unregister(self, writer):
        peer = writer.get_extra_info('peername')
        try:
            writer.close()
        except Exception:
            pass
        self.clients.discard(writer)
        log.info(f"Client disconnected: {peer}. Remaining: {len(self.clients)}")

    async def broadcast(self, data):
        for writer in list(self.clients):
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), timeout=2)
            except Exception:
                log.warning(f"Lost client {writer.get_extra_info('peername')}")
                self.unregister(writer)

    async def client_handler(self, reader, writer):
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.register(writer)

        async def from_client():
            try:
                while True:
                    data = await reader.read(1024)
                    if not data:
                        break
                    async with self.lock:
                        await self.remote.write(data)
            except Exception as e:
                log.warning(f"Client read error {peer}: {e}")

        async def keepalive():
            try:
                while True:
                    writer.send_iac(self.IAC + self.NOP)
                    await writer.drain()
                    await asyncio.sleep(self.heartbeat)
            except Exception:
                pass

        try:
            await asyncio.gather(from_client(), keepalive())
        finally:
            log.info(f"Closing client connection {peer}")
            self.unregister(writer)
            try:
                await writer.wait_closed()
            except Exception:
                pass


class ConnectionMuxer:
    def __init__(self, listen_ip, listen_port, remote_server, remote_port, heartbeat=30):
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.remote = RemoteSession(remote_server, remote_port, heartbeat)
        self.lock = asyncio.Lock()
        self.clients = TelnetManager(self.remote, self.lock, heartbeat)
        self.server = None

    async def start(self):
        asyncio.create_task(self.remote.read_loop(self.clients.broadcast))
        self.server = await telnetlib3.create_server(
            host=self.listen_ip,
            port=self.listen_port,
            shell=self.clients.client_handler
        )
        log.info(f"Telnet proxy listening on {self.listen_ip}:{self.listen_port}")
        await self.server.wait_closed()

    async def stop(self):
        await self.remote.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for client in list(self.clients.clients):
            self.clients.unregister(client)
        log.info("Multiplexer stopped")


if __name__ == "__main__":

    async def main():
        parser = argparse.ArgumentParser(description="Telnet Proxy Multiplexer")
        parser.add_argument("--remote-server", default="127.0.0.1", help="Remote server IP or hostname")
        parser.add_argument("--remote-port", type=int, default=5100, help="Remote server port")
        parser.add_argument("--listen-ip", default="0.0.0.0", help="IP address to listen on")
        parser.add_argument("--listen-port", type=int, default=5000, help="Port to listen on")
        parser.add_argument("--heartbeat", type=int, default=30, help="Heartbeat interval in seconds")
        args = parser.parse_args()

        multiplexer = ConnectionMuxer(
            listen_ip=args.listen_ip,
            listen_port=args.listen_port,
            remote_server=args.remote_server,
            remote_port=args.remote_port,
            heartbeat=args.heartbeat,
        )

        try:
            await multiplexer.start()
        except KeyboardInterrupt:
            log.info("Multiplexer closed")
            await multiplexer.stop()
        except Exception as e:
            log.error(f"Multiplexer failure: {e}")
            await multiplexer.stop()

    asyncio.run(main())
