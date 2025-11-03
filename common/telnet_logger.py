#!/usr/bin/env python3
import telnetlib3
import threading
import asyncio
import logging
from logging.handlers import RotatingFileHandler


def create_port_logger(port, max_size_mb=5, backup_count=5):
    logger = logging.getLogger(f"telnet_{port}")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = RotatingFileHandler(
            f"log_{port}.txt",
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count
        )
        formatter = logging.Formatter(
            '[%(asctime)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


async def telnet_logger(host, port):
    logger = create_port_logger(port)

    while True:
        try:
            logger.info(f"Connecting to Telnet on {host}:{port} ...")
            reader, writer = await telnetlib3.open_connection(host, port)
            logger.info("Logging output")
            break
        except (OSError, asyncio.TimeoutError, ConnectionRefusedError) as e:
            logger.warning(f"Connection failed: {e} Retrying in 3 seconds...")
            await asyncio.sleep(3)

    try:
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=1)
            except asyncio.TimeoutError:
                continue

            if line:
                logger.info(line.rstrip('\r\n'))

    except asyncio.CancelledError:
        logger.info("Logging stopped")
    except Exception as e:
        logger.error(f"ERROR: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        logger.info("Connection closed, restarting...")
        await telnet_logger(host, port)


def start_telnet_loggers(ports=None):
    if ports is None:
        logging.error("ERROR No Port Provided")
        return []

    host = '127.0.0.1'

    def run_loop():
        async def runner():
            tasks = [asyncio.create_task(telnet_logger(host, port)) for port in ports]
            await asyncio.gather(*tasks)

        asyncio.run(runner())

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    return thread
