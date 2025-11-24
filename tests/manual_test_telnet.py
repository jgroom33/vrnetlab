#!/usr/bin/env python3
"""
Manual test script for telnet_logger and telnetproxy.
Usage:
    # Test logger only
    python3 manual_test_telnet.py --test logger
    
    # Test proxy only
    python3 manual_test_telnet.py --test proxy
    
    # Test full stack (proxy + logger)
    python3 manual_test_telnet.py --test full
"""

import asyncio
import argparse
import sys
import time
import logging
from pathlib import Path
import glob
import os

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

import telnetlib3
from telnet_logger import start_telnet_loggers
from telnetproxy import ConnectionMuxer

LOG_FORMAT = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("manual_test")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.root.addHandler(console_handler)


class MockTelnetServer:
    """Mock telnet server for testing."""
    
    def __init__(self, port, data_rate=1):
        self.port = port
        self.data_rate = data_rate  # lines per second
        self.server = None
        self.clients = []
        
    async def handle_client(self, reader, writer):
        """Handle a client connection."""
        peer = writer.get_extra_info('peername')
        logger.info(f"Mock server: Client connected from {peer}")
        self.clients.append(writer)
        
        try:
            # Send welcome message
            writer.write(b"Mock Telnet Server Ready\r\n")
            await writer.drain()
            
            # Simulate continuous output
            line_num = 0
            while True:
                line_num += 1
                line = f"[{time.strftime('%H:%M:%S')}] Mock server output line {line_num}\r\n"
                writer.write(line.encode())
                await writer.drain()
                await asyncio.sleep(1.0 / self.data_rate)
                
        except Exception as e:
            logger.info(f"Mock server: Client {peer} disconnected: {e}")
        finally:
            self.clients.remove(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
    
    async def start(self):
        """Start the mock server."""
        self.server = await asyncio.start_server(
            self.handle_client,
            '127.0.0.1',
            self.port
        )
        logger.info(f"Mock server listening on 127.0.0.1:{self.port}")
        await self.server.serve_forever()
    
    async def stop(self):
        """Stop the mock server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for client in self.clients:
            client.close()


async def test_logger_only(port, duration=10):
    """Test telnet logger connecting to a mock server."""
    logger.info(f"=== Testing Logger Only (port={port}, duration={duration}s) ===")
    
    # Start mock server
    mock_server = MockTelnetServer(port, data_rate=2)
    server_task = asyncio.ensure_future(mock_server.start())
    await asyncio.sleep(1)  # Let server start
    
    # Start logger
    logger.info("Starting telnet logger...")
    start_telnet_loggers([port])
    
    # Run for specified duration
    logger.info(f"Running for {duration} seconds...")
    await asyncio.sleep(duration)
    
    # Stop mock server
    await mock_server.stop()
    server_task.cancel()
    
    # Wait for file handles to close
    await asyncio.sleep(0.5)
    
    # Clean up log files
    for log_file in glob.glob("log_*.txt*"):
        try:
            os.remove(log_file)
        except Exception:
            pass
    
    logger.info("=== Logger test complete ===")


async def test_proxy_only(listen_port, remote_port, duration=10):
    """Test telnet proxy."""
    logger.info(f"=== Testing Proxy Only (listen={listen_port}, remote={remote_port}, duration={duration}s) ===")
    
    # Start mock server on remote port
    mock_server = MockTelnetServer(remote_port, data_rate=2)
    server_task = asyncio.ensure_future(mock_server.start())
    await asyncio.sleep(1)
    
    # Start proxy
    logger.info("Starting telnet proxy...")
    muxer = ConnectionMuxer("127.0.0.1", listen_port, "127.0.0.1", remote_port)
    muxer_task = asyncio.ensure_future(muxer.start())
    await asyncio.sleep(1)
    
    # Connect a test client
    logger.info("Connecting test client...")
    try:
        reader, writer = await telnetlib3.open_connection("127.0.0.1", listen_port)
        
        # Read some data
        for _ in range(5):
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            logger.info(f"Client received: {line.strip()}")
        
        writer.close()
        await writer.wait_closed()
    except Exception as e:
        logger.error(f"Client error: {e}")
    
    # Stop proxy
    logger.info("Stopping proxy...")
    await muxer.stop()
    muxer_task.cancel()
    
    # Stop mock server
    await mock_server.stop()
    server_task.cancel()
    
    # Wait for file handles to close
    await asyncio.sleep(0.5)
    
    # Clean up log files
    for log_file in glob.glob("log_*.txt*") + ["telnet_multiplexer.log"]:
        try:
            os.remove(log_file)
        except Exception:
            pass
    
    logger.info("=== Proxy test complete ===")


async def test_full_stack(listen_port, remote_port, duration=15):
    """Test full stack: mock server -> proxy -> logger."""
    logger.info(f"=== Testing Full Stack (duration={duration}s) ===")
    
    # Start mock server
    mock_server = MockTelnetServer(remote_port, data_rate=1)
    server_task = asyncio.ensure_future(mock_server.start())
    await asyncio.sleep(1)
    
    # Start proxy
    logger.info("Starting proxy...")
    muxer = ConnectionMuxer("127.0.0.1", listen_port, "127.0.0.1", remote_port)
    muxer_task = asyncio.ensure_future(muxer.start())
    await asyncio.sleep(1)
    
    # Start logger (connects to proxy)
    logger.info("Starting logger...")
    log_manager = start_telnet_loggers([listen_port])
    await asyncio.sleep(2)
    
    # Run for duration
    logger.info(f"Running for {duration} seconds...")
    await asyncio.sleep(duration)
    
    # Check log file was created
    log_file = Path(f"log_{listen_port}.txt")
    if log_file.exists():
        size = log_file.stat().st_size
        logger.info(f"Log file created: {log_file} ({size} bytes)")
        # Show last few lines
        with open(log_file) as f:
            lines = f.readlines()
            logger.info(f"Last 3 lines of log:")
            for line in lines[-3:]:
                logger.info(f"  {line.rstrip()}")
    else:
        logger.error("Log file was not created!")
    
    # Stop everything
    logger.info("Stopping logger...")
    log_manager.stop()
    
    logger.info("Stopping proxy...")
    await muxer.stop()
    muxer_task.cancel()
    
    logger.info("Stopping mock server...")
    await mock_server.stop()
    server_task.cancel()
    
    # Wait for file handles to close
    await asyncio.sleep(0.5)
    
    # Clean up log files
    for log_file in glob.glob("log_*.txt*") + ["telnet_multiplexer.log"]:
        try:
            os.remove(log_file)
        except Exception:
            pass
    
    logger.info("=== Full stack test complete ===")


async def main():
    parser = argparse.ArgumentParser(description="Manual telnet testing")
    parser.add_argument("--test", choices=["logger", "proxy", "full"],
                       default="full", help="Test to run")
    parser.add_argument("--port", type=int, default=5000, help="Port for logger test")
    parser.add_argument("--listen-port", type=int, default=5000, help="Proxy listen port")
    parser.add_argument("--remote-port", type=int, default=5100, help="Proxy remote port")
    parser.add_argument("--duration", type=int, default=10, help="Test duration in seconds")
    
    args = parser.parse_args()
    
    try:
        if args.test == "logger":
            await test_logger_only(args.port, args.duration)
        elif args.test == "proxy":
            await test_proxy_only(args.listen_port, args.remote_port, args.duration)
        elif args.test == "full":
            await test_full_stack(args.listen_port, args.remote_port, args.duration)
    except KeyboardInterrupt:
        logger.info("\nTest interrupted by user")
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)


if __name__ == "__main__":
    # Python 3.6 compatibility
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
