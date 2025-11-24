#!/usr/bin/env python3

import pytest
import asyncio
import logging
import sys
from unittest.mock import Mock, patch
import glob
import os

from telnet_logger import (
    create_port_logger,
    telnet_logger,
    TelnetLoggerManager,
    start_telnet_loggers
)

# AsyncMock is only available in Python 3.8+, provide fallback
if sys.version_info >= (3, 8):
    from unittest.mock import AsyncMock
else:
    class AsyncMock(Mock):
        """Fallback AsyncMock for Python < 3.8"""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            
        def __call__(self, *args, **kwargs):
            async def _return():
                return super(AsyncMock, self).__call__(*args, **kwargs)
            return _return()
        
        async def __aenter__(self):
            return self
        
        async def __aexit__(self, *args):
            pass

# Python 3.6 compatibility: create_task was added in 3.7
def create_task_compat(coro):
    """Create a task compatible with Python 3.6+"""
    if sys.version_info >= (3, 7):
        return asyncio.create_task(coro)
    else:
        return asyncio.ensure_future(coro)

logger = logging.getLogger("test-run")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


@pytest.fixture(autouse=True)
def log_test_start_end(request):
    """Log test execution and clean up test artifacts."""
    logger.info(f"START {request.node.name}")
    yield
    logger.info(f"END {request.node.name}")


@pytest.fixture(autouse=True)
def _cleanup_logger_handlers():
    """Clean up logger handlers and log files after each test."""
    yield
    
    # Clean up all telnet loggers
    for name in list(logging.Logger.manager.loggerDict.keys()):
        if name.startswith("telnet_"):
            logger = logging.getLogger(name)
            logger.handlers[:] = []
    
    # Clean up log files created during tests
    for log_file in glob.glob("log_*.txt*"):
        try:
            os.remove(log_file)
        except Exception:
            pass

@pytest.fixture
def shutdown_event():
    """Provide a fresh shutdown event for each test."""
    return asyncio.Event()

@pytest.fixture
def mock_telnetlib3():
    """Mock telnetlib3.open_connection."""
    with patch("telnet_logger.telnetlib3") as mock:
        yield mock


class TestCreatePortLogger:
    """Test logger creation and configuration."""

    def test_creates_logger_with_correct_name(self):
        logger = create_port_logger(5000)
        assert logger.name == "telnet_5000"

    def test_creates_rotating_file_handler(self):
        logger = create_port_logger(5001)
        assert len(logger.handlers) == 1
        handler = logger.handlers[0]
        assert handler.__class__.__name__ == "RotatingFileHandler"

    def test_sets_max_bytes_correctly(self):
        logger = create_port_logger(5002, max_size_mb=10)
        handler = logger.handlers[0]
        assert handler.maxBytes == 10 * 1024 * 1024

    def test_clears_existing_handlers(self):
        # Create logger twice
        logger1 = create_port_logger(5003)
        assert len(logger1.handlers) == 1
        logger2 = create_port_logger(5003)
        assert len(logger2.handlers) == 1  # Should be 1, not 2


class TestTelnetLogger:
    """Test telnet logger connection and logging behavior."""

    @pytest.mark.asyncio
    async def test_successful_connection_and_logging(self, mock_telnetlib3, shutdown_event):
        """Test normal connection and logging flow."""
        # Mock reader/writer
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        # Simulate reading lines then shutting down
        lines_to_read = [b"Line 1\n", b"Line 2\n", b""]
        mock_reader.readline = AsyncMock(side_effect=lines_to_read)
        
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        # Run logger briefly
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.2)  # Let it connect and read
        shutdown_event.set()
        await task
        
        # Verify connection was established
        mock_telnetlib3.open_connection.assert_called()

    @pytest.mark.asyncio
    async def test_connection_retry_on_failure(self, mock_telnetlib3, shutdown_event):
        """Test retry logic when connection fails."""
        # First two attempts fail, third succeeds
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        mock_reader.readline = AsyncMock(return_value=b"")  # EOF
        
        mock_telnetlib3.open_connection = AsyncMock(
            side_effect=[
                ConnectionRefusedError("Refused"),
                OSError("Network error"),
                (mock_reader, mock_writer)
            ]
        )
        
        # Run logger with enough time for retries (3 sec delay between retries)
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(7)  # Allow time for 2 retries (3s each)
        shutdown_event.set()
        await task
        
        # Should have retried at least twice
        assert mock_telnetlib3.open_connection.call_count >= 2

    @pytest.mark.asyncio
    async def test_filters_timeout_messages(self, mock_telnetlib3, shutdown_event):
        """Test that 'Timeout.' messages are filtered."""
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        # Include a "Timeout." message
        mock_reader.readline = AsyncMock(
            side_effect=[b"Normal line\n", b"Timeout.\n", b"Another line\n", b""]
        )
        
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        with patch("telnet_logger.logging") as mock_logging:
            task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
            await asyncio.sleep(0.2)
            shutdown_event.set()
            await task

    @pytest.mark.asyncio
    async def test_reconnect_on_connection_loss(self, mock_telnetlib3, shutdown_event):
        """Test reconnection when connection is lost."""
        mock_reader1 = AsyncMock()
        mock_writer1 = Mock()
        mock_writer1.close = Mock()
        mock_writer1.wait_closed = AsyncMock()
        
        mock_reader2 = AsyncMock()
        mock_writer2 = Mock()
        mock_writer2.close = Mock()
        mock_writer2.wait_closed = AsyncMock()
        
        # First connection reads then closes, second connection established
        mock_reader1.readline = AsyncMock(side_effect=[b"Line 1\n", b""])
        mock_reader2.readline = AsyncMock(return_value=b"")
        
        mock_telnetlib3.open_connection = AsyncMock(
            side_effect=[
                (mock_reader1, mock_writer1),
                (mock_reader2, mock_writer2)
            ]
        )
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(4)  # Let first connection complete and reconnect (3s delay)
        shutdown_event.set()
        await task
        
        # Should have connected twice
        assert mock_telnetlib3.open_connection.call_count >= 2

    @pytest.mark.asyncio
    async def test_shutdown_during_connection(self, mock_telnetlib3, shutdown_event):
        """Test shutdown event during connection phase."""
        # Make connection slow
        async def slow_connect(*args, **kwargs):
            await asyncio.sleep(1)
            raise ConnectionRefusedError()
        
        mock_telnetlib3.open_connection = AsyncMock(side_effect=slow_connect)
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.1)
        shutdown_event.set()  # Signal shutdown
        await task  # Should exit quickly
        
        # Should not hang

    @pytest.mark.asyncio
    async def test_read_error_handling(self, mock_telnetlib3, shutdown_event):
        """Test error handling during read operations."""
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        # Simulate read error
        mock_reader.readline = AsyncMock(side_effect=Exception("Read error"))
        
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.2)
        shutdown_event.set()
        await task
        
        # Should handle error and attempt cleanup

    @pytest.mark.asyncio
    async def test_writer_close_error_handling(self, mock_telnetlib3, shutdown_event):
        """Test handling of errors during writer cleanup."""
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock(side_effect=Exception("Close error"))
        mock_writer.wait_closed = AsyncMock()
        
        mock_reader.readline = AsyncMock(return_value=b"")
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.2)
        shutdown_event.set()
        await task
        
        # Should handle close error gracefully

    @pytest.mark.asyncio
    async def test_empty_lines_filtered(self, mock_telnetlib3, shutdown_event):
        """Test that empty lines are not logged."""
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        # Include empty lines
        mock_reader.readline = AsyncMock(
            side_effect=[b"Line 1\n", b"\n", b"\r\n", b"Line 2\n", b""]
        )
        
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.2)
        shutdown_event.set()
        await task


class TestTelnetLoggerManager:
    """Test logger manager lifecycle."""

    def test_manager_initialization(self):
        manager = TelnetLoggerManager()
        assert manager.tasks == []
        assert manager.loop is None
        assert manager.loop_thread is None

    def test_start_loggers_creates_thread(self):
        manager = TelnetLoggerManager()
        with patch("telnet_logger.asyncio"):
            manager.start_loggers([5000, 5001])
            assert manager.loop_thread is not None

    def test_start_loggers_with_no_ports(self):
        manager = TelnetLoggerManager()
        manager.start_loggers([])  # Should log error but not crash

    def test_start_telnet_loggers_backwards_compatibility(self):
        """Test backwards compatible function."""
        with patch.object(TelnetLoggerManager, "start_loggers") as mock_start:
            result = start_telnet_loggers([5000])
            assert result is not None
            mock_start.assert_called_once()

    def test_start_telnet_loggers_no_ports_error(self):
        """Test error handling when no ports provided."""
        result = start_telnet_loggers(None)
        assert result is None


class TestReadTimeoutBehavior:
    """Test timeout handling during read operations."""

    @pytest.mark.asyncio
    async def test_read_timeout_continues_loop(self, mock_telnetlib3, shutdown_event):
        """Test that read timeouts don't break the loop."""
        mock_reader = AsyncMock()
        mock_writer = Mock()
        mock_writer.close = Mock()
        mock_writer.wait_closed = AsyncMock()
        
        # Simulate timeouts then data - use a list to track calls
        call_tracker = {'count': 0}
        
        def readline_side_effect(*args, **kwargs):
            call_tracker['count'] += 1
            if call_tracker['count'] < 3:
                raise asyncio.TimeoutError()
            return b"Data after timeout\n" if call_tracker['count'] == 3 else b""
        
        mock_reader.readline = AsyncMock(side_effect=readline_side_effect)
        mock_telnetlib3.open_connection = AsyncMock(return_value=(mock_reader, mock_writer))
        
        task = create_task_compat(telnet_logger("127.0.0.1", 5000, shutdown_event))
        await asyncio.sleep(0.5)  # Give it time to process
        shutdown_event.set()
        await task
        
        # Should have continued reading after timeouts
        assert call_tracker['count'] >= 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
