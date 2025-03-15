#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import vrnetlab


def handle_SIGCHLD(_signal, _frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class SAOS_vm(vrnetlab.VM):

    variant_map = {
        "5132": {
            "interface_count" : 4
        }
    }

    def __init__(self, hostname, username, password, conn_mode, variant):
        disk_image = "/"
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
                break
        super(SAOS_vm, self).__init__(
            username, password, disk_image=disk_image, ram=8196, cpu="host", smp="2,sockets=1,cores=1,threads=2"
        )

        self.hostname = hostname
        self.variant = variant
        self.variant_data = SAOS_vm.variant_map.get(variant)
        if self.variant_data is None:
            raise Exception("Unsupported variant")

        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = self.variant_data["interface_count"]
        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}", 
                "-machine",
                "smm=off",
                "-boot",
                "order=c",
                "-drive",
                "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd", 
                "-drive",
                "if=pflash,format=raw,file=/usr/share/OVMF/OVMF_VARS.fd", 
                "-uuid",
                "6af6dbea-ac21-4fd0-a796-5313611f8147", #fix hard code value later
                "-net",
                "none",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product=CN{self.variant},serial=SIM6af6dbea-ac21-4fd0-a796-5313611f8147",  #fix hard code value later
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname},value=is-sim:true,value=locationId:0,value=jsonData:{{\\\"variant\\\":\\\"CN{self.variant}\\\"}}",  #fix hard code value later
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

    def startup_config(self):
        """Provide initial node configuration"""
        return
        # FIXME: need to debug this logic
        self.logger.info("applying configuration")
        self.wait_write("config", wait=r'\S+>')
        self.wait_write(f"system config hostname {self.hostname}", wait=r'\S+#')
        self.wait_write("exit", wait=r'\S+#')
        self.logger.info("applying configuration done")


class SAOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        variant = "5132"  # FIXME: need to parameterize this
        super().__init__(username, password)
        self.vms = [SAOS_vm(hostname, username, password, conn_mode, variant)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", help="hostname")
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
    vr.start()
