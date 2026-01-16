#!/usr/bin/env python3
import telnetlib3
import asyncio
import inspect
import logging
from logging.handlers import RotatingFileHandler


def silence_telnetlib3_logging():
    """Suppress telnetlib3 and asyncio DEBUG output from docker logs."""
    for logger_name in ["telnetlib3", "asyncio"]:
        lib_logger = logging.getLogger(logger_name)
        lib_logger.setLevel(logging.WARNING)


silence_telnetlib3_logging()


def create_port_logger(port, max_size_mb=5, backup_count=5):
    """Create a logger for a specific port with rotating file handler."""
    logger = logging.getLogger(f"telnet_{port}")
    logger.setLevel(logging.INFO)

    # Clear any existing handlers to prevent accumulation
    logger.handlers.clear()

    # Prevent propagation to root logger (and docker logs)
    logger.propagate = False

    handler = RotatingFileHandler(
        f"log_{port}.txt", maxBytes=max_size_mb * 1024 * 1024, backupCount=backup_count
    )
    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


async def telnet_logger(host, port, shutdown_event):
    """Connect to telnet and log output with proper cleanup."""
    logger = create_port_logger(port)
    reader = None
    writer = None
    first_connect = True

    while not shutdown_event.is_set():
        try:
            # Connection phase with retry logic
            while not shutdown_event.is_set():
                try:
                    if first_connect:
                        logger.info(f"Connecting to Telnet on {host}:{port} ...")
                    reader, writer = await asyncio.wait_for(
                        telnetlib3.open_connection(
                            host, port, connect_minwait=0.0, connect_maxwait=0.0
                        ),
                        timeout=10,
                    )
                    if first_connect:
                        logger.info("Connected - logging output")
                        first_connect = False
                    break
                except (OSError, asyncio.TimeoutError, ConnectionRefusedError) as e:
                    logger.warning(f"Connection failed: {e}. Retrying in 3 seconds...")
                    await asyncio.sleep(3)

            if shutdown_event.is_set():
                break

            # Logging phase - telnetlib3 has hardcoded 300s timeout, handle it gracefully
            while not shutdown_event.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=1)
                    if not line:
                        # Connection closed - exit quietly to reconnect
                        break
                    # Filter out telnetlib3 timeout messages to prevent log clutter
                    line_stripped = line.rstrip("\r\n")
                    if line_stripped and line_stripped != "Timeout.":
                        logger.info(line_stripped)
                except asyncio.TimeoutError:
                    # Short timeout for responsiveness, continue reading
                    continue
                except Exception as e:
                    logger.error(f"Read error: {e}")
                    break

        except asyncio.CancelledError:
            logger.info("Logger task cancelled")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            await asyncio.sleep(3)
        finally:
            # Clean up connection resources
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception as e:
                    logger.debug(f"Error closing writer: {e}")
                writer = None
            reader = None

            if not shutdown_event.is_set():
                # No log message for normal telnetlib3 timeout behavior
                await asyncio.sleep(3)

    logger.info("Logger stopped")
    # Clean up logger handlers
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)


class TelnetLoggerManager:
    """Manages multiple telnet logger tasks with proper lifecycle management."""

    def __init__(self):
        self.tasks = []
        self.shutdown_event = asyncio.Event()
        self.loop_thread = None
        self.loop = None

    def start_loggers(self, ports, host="127.0.0.1"):
        """Start telnet loggers for specified ports in a separate thread."""
        if not ports:
            logging.error("ERROR: No ports provided")
            return

        import threading

        def run_loop():
            # Create a new event loop for this thread
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            try:
                # Create tasks for each port
                self.tasks = []
                for port in ports:
                    coro = telnet_logger(host, port, self.shutdown_event)
                    task = self.loop.create_task(coro)
                    if inspect.isawaitable(task):
                        self.tasks.append(task)
                    else:
                        coro.close()
                if not self.tasks:
                    return

                # Run until shutdown
                self.loop.run_until_complete(
                    asyncio.gather(*self.tasks, return_exceptions=True)
                )
            except Exception as e:
                logging.error(f"Logger loop error: {e}")
            finally:
                # Clean up all tasks
                for task in self.tasks:
                    if not task.done():
                        task.cancel()

                # Wait for cancellations to complete
                if self.tasks:
                    self.loop.run_until_complete(
                        asyncio.gather(*self.tasks, return_exceptions=True)
                    )

                self.loop.close()
                self.loop = None

        self.loop_thread = threading.Thread(target=run_loop, daemon=True)
        self.loop_thread.start()

        return self


def start_telnet_loggers(ports=None):
    """Start telnet loggers for specified ports."""
    if ports is None:
        logging.error("ERROR: No Port Provided")
        return None

    manager = TelnetLoggerManager()
    manager.start_loggers(ports)
    return manager


def start_telnet_infrastructure(vms, logger):
    """Setup telnet multiplexing and logging for all VM consoles
    QEMU VMs listen on ports: 5100, 5101, 5102, ... (VM.num based)
    Proxies listen on ports:  5000, 5001, 5002, ... (external access)
    Logs written to: log_5000.txt, log_5001.txt, ...
    """
    import subprocess

    num_vms = len(vms)
    logger.info(f"Setting up telnet infrastructure for {num_vms} VM(s)")

    # Start a telnet proxy subprocess for each VM
    for vm in vms:
        listen_port = 5000 + vm.num
        remote_port = 5100 + vm.num

        cmd = [
            "uv",
            "run",
            "telnetproxy.py",
            "--remote-server",
            "127.0.0.1",
            "--remote-port",
            str(remote_port),
            "--listen-port",
            str(listen_port),
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(
            f"Telnet proxy for {vm.name}: external port {listen_port} -> QEMU port {remote_port}"
        )

    # Start telnet loggers for each console (connects to proxy ports)
    logger_ports = [5000 + vm.num for vm in vms]
    start_telnet_loggers(ports=logger_ports)
    logger.info(f"Console logging active for ports: {logger_ports}")
    logger.info(f"Log files: {', '.join([f'log_{p}.txt' for p in logger_ports])}")
