#!/usr/bin/env python3

import datetime
import time
import logging
import os
import re
import signal
import sys
import vrnetlab
import uuid
import socket
import resource
import subprocess
import asyncio
from telnet_logger import start_telnet_loggers
proxy_port = 5000


def handle_SIGCHLD(_signal, _frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

SOFT_ULIMIT_NOFILE = 524288  # from containerlab ulimit on Oracle linux 10
TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class SAOS_vm(vrnetlab.VM):

    variant_map = {
        "3948": {
            "interface_count"       : 16,
        },
        "3984": {
            "interface_count"       : 6,
        },
        "3985": {
            "interface_count"       : 6,
        },
        "5130": {
            "interface_count"       : 14,
        },
        "5131": {
            "interface_count"       : 14,
        },
        "5132": {
            "interface_count"       : 4,
        },
        "5134": {
            "interface_count"       : 26,
        },
        "5144": {
            "interface_count"       : 28,
        },
        "5162": {
            "interface_count"       : 42,
        },
        "5164": {
            "interface_count"       : 36,
        },
        "5166": {
            "interface_count"       : 34,
        },
        "5168": {
            "interface_count"       : 36,
        },
        "5169": {
            "interface_count"       : 16,
        },
        "5170": {
            "interface_count"       : 44,
        },
        "5171": {
            "interface_count"       : 56,
        },
        "5184": {
            "interface_count"       : 36,
        },
        "5186": {
            "interface_count"       : 12,
        },
        "8110": {
            "interface_count"       : 58,
        },
        "8112": {
            "interface_count"       : 40,
        },
        "8114": {
            "interface_count"       : 78,
        },
        "8140": {
            "interface_count"       : 48,  # no CPU ports
        },
        "8190": {
            "interface_count"       : 36,  # no CPU ports
        },
        "8192": {
            "interface_count"       : 36,  # no CPU ports
        },
    }

    def __init__(self, hostname, username, password, conn_mode):
        disk_image = "/"
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
                break

        self.variant = os.environ.get("CLAB_LABEL_CLAB_NODE_TYPE")

        if self.variant is None:
            raise Exception("Missing saos variant in the yml file.")

        self.variant_data = SAOS_vm.variant_map.get(self.variant)
        if self.variant_data is None:
            raise Exception(f"Unsupported variant: {self.variant}")

        # NOTE: Can not use logger until superclass constructor is complete
        super(SAOS_vm, self).__init__(
            username, password, disk_image=disk_image, ram=8196, cpu="host", smp="2,sockets=1,cores=1,threads=2",
        )

        for i, arg in enumerate(self.qemu_args):
            if arg.startswith("telnet:0.0.0.0:50"):
                qemu_port = arg.replace("telnet:0.0.0.0:50", "telnet:0.0.0.0:51")
                self.logger.info(f"Overriding QEMU telnet port to {qemu_port}")
                self.qemu_args[i] = qemu_port
                break

        self.logger.info(f"Variant: {self.variant}")

        # 179 - BGP
        # 225 - debug shell
        # 4243 - docker daemon
        self.mgmt_tcp_ports.extend([179, 225, 4243])

        self.hostname = hostname

        self.uuid = str(uuid.uuid4())
        self.serial_number = f"SIM{self.uuid}"[0:8]  # create an 8 character serial number

        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = self.variant_data["interface_count"]

        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off,smm=off",
                "-boot",
                "order=c",
                "-bios",
                "/usr/share/OVMF/OVMF_CODE.fd",
                "-uuid",
                f"{self.uuid}",
                "-net",
                "none",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product=CN{self.variant},serial={self.serial_number}",
            f"type=2,manufacturer=Ciena,product=CN{self.variant}",
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname}," +
            f"value=is-sim:true,value=locationId:0,value=jsonData:{{\\\"variant\\\":\\\"CN{self.variant}\\\"}}",
        ]
        self.hostname = hostname

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.logger.info("To many spins with no result, restarting")
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.tn.expect([b"login:"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.debug("matched login prompt")
                self.logger.debug("trying to log in with 'diag'")
                self.wait_write("diag", wait=None)
                self.wait_write("ciena123", wait="Password:")
                self.logger.debug("login complete")

                # run config commands
                if self.mgmt_passthrough:
                    self.bootstrap_config()
                self.startup_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info(f"Startup complete in: {startup_time}")
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace(f"OUTPUT: {res.decode()}")
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def bootstrap_config(self):
        """Do the actual bootstrap config"""
        new_password = False
        ipv4, subnet = self.mgmt_address_ipv4.split('/')
        wait_strings = ["BASE-dnx-SIM>", "BASE-dnx-SIM?>"]

        self.logger.info("applying bootstrap configuration")
        for i, wait_str in enumerate(wait_strings):
            op = self.wait_write("show bootstrap-status", wait=wait_str)
            if "Bootstrap Done" in op:
                break

        while True:
            op = self.wait_write("show bootstrap-status", wait=wait_strings[-1])
            time.sleep(5)
            if "Bootstrap Done" in op:
                break

        self.wait_write("exit", wait=wait_strings[-1])
        self.logger.debug("trying to log in with 'diag'")
        self.wait_write("diag", wait="login:")
        self.wait_write("ciena123", wait="Password:")
        self.logger.debug("login completed for bootstrap config")

        op = self.wait_write("config", wait=f"{self.variant}>")
        # newer load has the string "password" in the output
        if "password" in op:
            new_password = True

        # newer load requires password change
        if new_password:
            self.wait_write("system aaa authentication users user diag config password ciena1234")
            self.logger.debug("trying to log in with 'diag'")
            self.wait_write("diag", wait="login:")
            self.wait_write("ciena1234", wait="Password:")
            self.logger.debug("login completed after password change")
            self.wait_write("config", wait=f"{self.variant}>")
            # change password back to default password
            self.wait_write("system aaa authentication users user diag config password ciena123")

        self.wait_write(f"system config hostname {self.hostname}")
        self.wait_write("dhcp-client client mgmtbr0 admin-enable false")
        self.wait_write(f"oc-if:interfaces interface mgmtbr0 ipv4 addresses address {ipv4} config ip {ipv4} prefix-length {subnet}")
        self.wait_write(f"rib vrf default ipv4 0.0.0.0/0 next-hop {re.sub(r'\d+$', '1', ipv4)}")
        self.wait_write("exit")
        self.wait_write("exit")
        self.wait_write("exit")
        self.wait_write("exit")

    def startup_config(self):
        """Load additional config provided by user."""
        return


class SAOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)
        self.vms = [SAOS_vm(hostname, username, password, conn_mode)]

        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            new_soft = min(SOFT_ULIMIT_NOFILE, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            self.logger.info(f"ulimit -n set: soft={soft}, hard={hard}, new_soft={new_soft}")
        except ValueError as ve:
            self.logger.warning(f"Invalid value for ulimit -n: {ve}")
        except PermissionError as pe:
            self.logger.warning(f"Permission denied when setting ulimit -n: {pe}")
        except Exception as e:
            self.logger.warning(f"Unexpected error setting ulimit -n: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default=socket.gethostname(), help="hostname")
    parser.add_argument("--username", default="diag", help="username (not used)")
    parser.add_argument("--password", default="ciena123", help="password (not used)")
    parser.add_argument(
        "--connection-mode", default="tc", help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = SAOS(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    cmd = [
            "uv",
            "run",
            "telnetproxy.py",
            "--remote-server",  "127.0.0.1",
            "--remote-port", "5100",
            "--listen-port", str(proxy_port)
    ]
    subprocess.Popen(cmd)
    start_telnet_loggers(ports=[proxy_port])
    vr.start()
