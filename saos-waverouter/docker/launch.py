#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys
import vrnetlab
import uuid
import socket

OVMF_VARS_gz = "/backup/OVMF_VARS_bkup.fd.gz"
OVMF_VARS = "/backup/OVMF_VARS_bkup.fd"
WR1_CTM_AP = "/WR1-CTM_ap.img.tar" #change variable name later


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


def gen_shared_mac(housing_id, location_id, second_last_octet, last_octet):
    return "52:54:%02x:%02x:%02x:%02x" % (
        housing_id,
        location_id,
        second_last_octet,
        last_octet,
)


logging.Logger.trace = trace


class WR_ctm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = "/"
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
                break

        # NOTE: Can not use logger until superclass constructor is complete
        super(WR_ctm, self).__init__(
            username, password, disk_image=disk_image, ram=10240, cpu="host", smp="4,sockets=1,dies=1,cores=2,threads=2",
        )

        self.variant = "wr-ctm"
        self.product_number = "ne26xqsfp28"
        self.housing_id =  "1"
        self.location_id = "7"

        # 179 - BGP
        # 225 - debug shell
        self.mgmt_tcp_ports.extend([179, 225])

        self.hostname = hostname + "-" + self.variant

        self.uuid = str(uuid.uuid4())
        self.serial_number = f"SIM{self.uuid}"[0:8]  # create a 8-character serial number

        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = 0

        self.logger.debug("Unzip OVMF_VARS_bkup.fd.gz...")
        if not os.path.exists(OVMF_VARS_gz):
            raise Exception(f"File {OVMF_VARS_gz} not found")
        vrnetlab.run_command(["gunzip", "/backup/OVMF_VARS_bkup.fd.gz"])

        self.logger.debug(f"Making copy of OVMF_VARS for {self.variant}...")
        if not os.path.exists(OVMF_VARS):
            raise Exception(f"File {OVMF_VARS} not found")
        vrnetlab.run_command(["cp", "/backup/OVMF_VARS_bkup.fd", f"/OVMF_VARS_ctm.fd"])

        self.logger.debug("Extracting WR1_CTM_AP...")
        if not os.path.exists(WR1_CTM_AP):
            raise Exception(f"File {WR1_CTM_AP} not found")
        vrnetlab.run_command(["tar", "xSf", WR1_CTM_AP])

        self.provision_pci_bus = False #to stop clab setting up PCI buses

        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off,smm=off,usb=off,memory-backend=pc.ram",
                "-boot",
                "strict=on",
                "-object",
                "qom-type=memory-backend-ram,id=pc.ram,size=10737418240",
                "-device",
                "pcie-root-port,port=40,chassis=1,id=pci.1,bus=pcie.0,multifunction=on,addr=0x5",
                "-device",
                "pcie-root-port,port=41,chassis=2,id=pci.2,bus=pcie.0,addr=0x5.0x1",
                "-drive",
                "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd",
                "-drive",
                "if=pflash,format=raw,file=/OVMF_VARS_ctm.fd",
                "-drive",
                "if=none,file=/WR1-CTM_ap.img,format=raw,id=disk2",
                "-device",
                "ide-hd,bus=ide.1,drive=disk2,id=sata0-0-1",
                "-net", 
                "none",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product={self.product_number},serial={self.serial_number}",
            f"type=2,manufacturer=Ciena,product={self.product_number}", 
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname}," +
            f"value=is-sim:true,value=locationId:{self.location_id},value=jsonData:{{\"variant\":\"{self.variant}\"}},value=is-dual-ctm:no", #fix hard coded value later
        ]

        self.if_name = self.variant + "-" + self.housing_id + self.location_id + "bp"
        self.if_num = 5 #number of backplane interfaces
        self.if_list = [f"{self.if_name}{i}" for i in range(self.if_num)]

    def start(self):
        # use parent class start() function
        super(WR_ctm, self).start()

        # add interface to internal control plane bridge
        self.logger.debug(f"Adding backplane interfaces from {self.variant} into the bridge...")
        for i in range(self.if_num):
            vrnetlab.run_command(["brctl", "addif", "int_cp", f"{self.if_list[i]}"])
            vrnetlab.run_command(["ip", "link", "set", f"{self.if_list[i]}", "up"])

    def gen_mgmt(self):
        """Generate mgmt interface(s)

        We override the default function for the wr-ctm
        """
        res = super(WR_ctm, self).gen_mgmt()

        replace_index = res.index("virtio-net-pci,netdev=p00,mac=%s" % self.mgmt_mac)
        res[replace_index] += ",multifunction=on,addr=0x3"

        # mgmt interface
        res.append("-device")
        res.append("virtio-net-pci,netdev=p01,mac=%s,bus=pcie.0,addr=0x3.0x1" % (self.mgmt_mac[:-1] + "1"))
        res.append("-netdev")
        res.append("tap,ifname=wr-mgmt,id=p01,script=no,downscript=no")

        # add virtio NIC for internal control plane interface to wr-ctm
        res.append("-device")
        res.append("virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,multifunction=on,addr=0x4" % (self.if_list[0], gen_shared_mac(int(self.housing_id),  int(self.location_id), 2, 0)))
        res.append("-netdev")
        res.append("tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[0], self.if_list[0]))

        for i in range(1, self.if_num):
            res.append("-device")
            res.append("virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,addr=0x4.0x%s" % (self.if_list[i], gen_shared_mac(int(self.housing_id),  int(self.location_id), i+2, i), i))
            res.append("-netdev")
            res.append("tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[i], self.if_list[i]))

        # might not need this - can be removed later
        res.append("-object")
        res.append("qom-type=rng-random,id=objrng0,filename=/dev/urandom")
        res.append("-device")
        res.append("virtio-rng-pci,rng=objrng0,id=rng0,bus=pci.1,addr=0x0")

        return res

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
                self.logger.info(f"Startup for {self.variant} complete in: {startup_time}")
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


class WR_qb(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = "/qbox-disk/"
        for e in os.listdir("/qbox-disk/"):
            if re.search(".qcow2$", e):
                disk_image = "/qbox-disk/" + e
                break

        # NOTE: Can not use logger until superclass constructor is complete
        super(WR_qb, self).__init__(
            username, password, disk_image=disk_image, ram=5120, cpu="host", smp="4,sockets=1,dies=1,cores=2,threads=2", num=1 #num - vm number
        )

        self.variant = "wr-qb"
        self.product_number = "qb615xqsfpdd"
        self.housing_id =  "1"
        self.location_id = "5"

        self.hostname = hostname + "-" + self.variant

        self.uuid = str(uuid.uuid4())
        self.serial_number = f"SIM{self.uuid}"[0:8]  # create a 8-character serial number

        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = 0

        if not os.path.exists(OVMF_VARS):
            raise Exception(f"File {OVMF_VARS} not found")
        self.logger.debug(f"Making copy of OVMF_VARS for {self.variant}...")
        vrnetlab.run_command(["cp", "/backup/OVMF_VARS_bkup.fd", "/OVMF_VARS_qb.fd"])

        self.provision_pci_bus = False #to stop setting up PCI buses

        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off,smm=off,usb=off,memory-backend=pc.ram",
                "-boot",
                "strict=on",
                "-object",
                "qom-type=memory-backend-ram,id=pc.ram,size=5368709120", 
                "-device",
                "pcie-root-port,port=40,chassis=1,id=pci.1,bus=pcie.0,multifunction=on,addr=0x5",
                "-device",
                "pcie-root-port,port=41,chassis=2,id=pci.2,bus=pcie.0,addr=0x5.0x1",
                "-drive",
                "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd",
                "-drive",
                "if=pflash,format=raw,file=/OVMF_VARS_qb.fd",
                "-net",
                "none",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product={self.product_number},serial={self.serial_number}",
            f"type=2,manufacturer=Ciena,product={self.product_number}", 
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname}," +
            f"value=is-sim:true,value=locationId:{self.location_id},value=jsonData:{{\"variant\":\"{self.variant}\"}},value=is-dual-ctm:no", #fix hard coded value later
        ]

        self.if_name = self.variant + "-" + self.housing_id + self.location_id + "bp"
        self.if_num = 6 #number of backplane interfaces
        self.if_list = [f"{self.if_name}{i}" for i in range(self.if_num)]
    
    def start(self):
        # use parent class start() function
        super(WR_qb, self).start()

        # add interface to internal control plane bridge
        self.logger.debug(f"Adding backplane interfaces from {self.variant} into the bridge...")
        for i in range(self.if_num):
            vrnetlab.run_command(["brctl", "addif", "int_cp", f"{self.if_list[i]}"])
            vrnetlab.run_command(["ip", "link", "set", f"{self.if_list[i]}", "up"])

    def gen_mgmt(self):
        """Generate mgmt interface(s)

        We override the default function for the wr-qb
        """
        res = super(WR_qb, self).gen_mgmt()

        replace_index = res.index("virtio-net-pci,netdev=p00,mac=%s" % self.mgmt_mac)
        res[replace_index] += ",multifunction=on,addr=0x3"

        res[replace_index + 2] = "user,id=p00,net=10.0.0.0/24,host=10.0.0.2,dns=10.0.0.3,dhcpstart=10.0.0.16"

        # add virtio NIC for internal control plane interface to wr-qb #DIFF
        res.append("-device")
        res.append("virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,multifunction=on,addr=0x4" % (self.if_list[0], gen_shared_mac(int(self.housing_id),  int(self.location_id), 2, 0)))
        res.append("-netdev")
        res.append("tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[0], self.if_list[0]))

        for i in range(1, self.if_num):
            res.append("-device")
            res.append("virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,addr=0x4.0x%s" % (self.if_list[i], gen_shared_mac(int(self.housing_id),  int(self.location_id), i+2, i), i))
            res.append("-netdev")
            res.append("tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[i], self.if_list[i]))

        # might not need this - can be removed later
        res.append("-object")
        res.append("qom-type=rng-random,id=objrng0,filename=/dev/urandom")
        res.append("-device")
        res.append("virtio-rng-pci,rng=objrng0,id=rng0,bus=pci.1,addr=0x0")

        return res


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
                self.logger.info(f"Startup for {self.variant} complete in: {startup_time}")
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


class WR(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)
        self.vms = [WR_ctm(hostname, username, password, conn_mode), WR_qb(hostname, username, password, conn_mode)]

        # set up bridge for connecting wr-ctm and wr-qb
        self.logger.debug("Creating linux bridge...")
        vrnetlab.run_command(["brctl", "addbr", "int_cp"])
        vrnetlab.run_command(["ip", "link", "set", "int_cp", "up"])

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

    vr = WR(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    vr.start()
