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
import json
import tempfile
from disk import PartitionInfo, create_disk_image

OVMF_VARS_gz = "/backup/OVMF_VARS_bkup.fd.gz"
OVMF_VARS = "/backup/OVMF_VARS_bkup.fd"
CTM_AP_gz = "/CTM_ap.img.tar.gz"
CTM_AP = "/CTM_ap.img.tar"
LINUX_BRIDGE = "int_cp"
HOUSING_POOL_MAX = "1"
IPV4_ADDR_OCTET4_BASE = 21


def handle_SIGCHLD(_signal, _frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

node_uuid = str(uuid.uuid4())
node_serial_number = f"SIM{node_uuid}"[0:8]
node_mgmt_mac = vrnetlab.gen_mac(0)


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


def create_instance_disk(override_dicts, disk_name):
    # instance disk creation
    raw_disk_path = f"/instance_disk/{disk_name}.img"

    instance_data_dir = f"{disk_name}_dir"

    with tempfile.TemporaryDirectory(prefix=instance_data_dir) as tempdir:
        instance_data_files = []
        for key, value in override_dicts.items():
            instance_data_file = f"{tempdir}/{key}.json"
            instance_data_files.append(instance_data_file)

            # serializing json
            json_object = json.dumps(value, indent=4)

            with open(instance_data_file, "w") as json_file:
                json_file.write(json_object)

            with open(instance_data_file, 'r') as file:
                file_content = file.read()
            data = json.loads(file_content)

            logger.info(f"{instance_data_file} from temporary dir")
            logger.debug(data)

        instance_partition = [
            PartitionInfo(1, "INSTANCE-DATA", "128M", 0x8300, "ext4", instance_data_files)
        ]
        create_disk_image(raw_disk_path, "256M", instance_partition, disk_name)

        convert_cmd = ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", f"{raw_disk_path}", f"/instance_disk/{disk_name}.qcow2"]
        logger.debug("Img to qcow2 command: %s" % ' '.join(convert_cmd))
        if not os.path.exists(raw_disk_path):
            raise Exception(f"File {raw_disk_path} not found")
        vrnetlab.run_command(convert_cmd)


logging.Logger.trace = trace


class WR_base(vrnetlab.VM):
    def __init__(
        self,
        hostname,
        username,
        password,
        conn_mode,
        num,
        housing_id,
        location_id,
        variant,
        product_number,
        size,
        num_backplane_if,
        ram,
        num_nics,
        smp
    ):

        disk_image = "/"
        for e in os.listdir("/"):
            # search for the qcow2 disk image which isn't the overlays
            if re.search(r"^(?!.*-overlay).*\.qcow2$", e):
                disk_image = os.path.join("/", e)
                break

        # NOTE: Can not use logger until superclass constructor is complete
        super(WR_base, self).__init__(
            username, password, disk_image=disk_image, ram=ram, cpu="host",
            smp=smp, num=num
        )

        self.variant = variant
        self.product_number = product_number
        self.housing_id = housing_id
        self.location_id = location_id
        self.hostname = hostname + "-" + self.variant
        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = num_nics
        self.uuid = str(uuid.uuid4())
        self.serial_number = f"SIM{self.uuid}"[0:8]
        self.mgmt_mac = vrnetlab.gen_mac(0)
        self.name = f"{self.variant}_{self.housing_id}_{self.location_id}"

        self.logger.info(f"Changing overlay disk image name for vm{self.num}")
        base, ext = os.path.splitext(disk_image)
        base_overlay = base + "-overlay"
        new_overlay = base_overlay + f"-{self.num}" + ext
        vrnetlab.run_command(["mv", f"{base_overlay+ext}", new_overlay])
        self.logger.debug(f"mv {base_overlay+ext} {new_overlay}")
        replace_index = self.qemu_args.index(
            f"if=ide,file={base_overlay+ext}")
        self.qemu_args[replace_index] = f"if=ide,file={new_overlay}"

        self.logger.info(f"Making copy of OVMF_VARS for {self.name}...")
        if not os.path.exists(OVMF_VARS):
            raise Exception(f"File {OVMF_VARS} not found")
        vrnetlab.run_command(["cp", OVMF_VARS, f"/OVMF_VARS_{self.num}.fd"])

        self.provision_pci_bus = False  # to stop clab setting up PCI buses

        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off,smm=off,usb=off,memory-backend=pc.ram",
                "-boot",
                "strict=on",
                "-object",
                f"qom-type=memory-backend-ram,id=pc.ram,size={size}",
                "-device",
                "pcie-root-port,port=40,chassis=1,id=pci.1,bus=pcie.0,multifunction=on,addr=0x5",
                "-device",
                "pcie-root-port,port=41,chassis=2,id=pci.2,bus=pcie.0,addr=0x5.0x1",
                "-drive",
                "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd",
                "-drive",
                f"if=pflash,format=raw,file=/OVMF_VARS_{num}.fd",
                "-net",
                "none",
                # might not need this - can be removed later
                "-object",
                "qom-type=rng-random,id=objrng0,filename=/dev/urandom",
                "-device",
                "virtio-rng-pci,rng=objrng0,id=rng0,bus=pci.1,addr=0x0",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product={self.product_number},serial={self.serial_number}",
            f"type=2,manufacturer=Ciena,product={self.product_number}",
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname}," +
            f"value=is-sim:true,value=locationId:{self.location_id},value=jsonData:{{\"variant\":\"{self.variant}\"}},value=is-dual-ctm:no",  # fix hard coded value later
        ]

        self.if_name = self.name + "bp"
        self.num_backplane_if = num_backplane_if
        self.if_list = [f"{self.if_name}{i}"
                        for i in range(self.num_backplane_if)]

        self.logger.info(f"HOUSING POOL: {HOUSING_POOL_MAX}")

        override_dicts = {
            "override_software": {
                "SOFTWARE": {
                    "NN": f"{os.environ.get("CLAB_LABEL_CLAB_NODE_NAME")}"
                },
                "HOUSING": {
                    "HOUSING_ID": f"{self.housing_id}",
                    "HOUSING_POOL": f"{HOUSING_POOL_MAX}"
                }
            },
            "override_sid": {
                "MFG": {
                    "MS": f"{node_serial_number}",
                    "EA": f"{node_mgmt_mac}",
                    "EA2": f"{self.mgmt_mac[:-1]}4"
                }
            }
        }

        create_instance_disk(override_dicts, self.name)

        self.qemu_args.extend(
            [
                "-drive",
                f"if=none,file=/instance_disk/{self.name}.qcow2,format=qcow2,id=disk3",
                "-device",
                "ide-hd,bus=ide.2,drive=disk3,id=sata0-0-2",
            ]
        )

    def start(self):
        # use parent class start() function
        super(WR_base, self).start()

        # add interface to internal control plane bridge
        self.logger.info(f"Adding backplane interfaces from {self.name} into the bridge...")
        for i in range(self.num_backplane_if):
            vrnetlab.run_command(["brctl", "addif", f"{LINUX_BRIDGE}", f"{self.if_list[i]}"])
            vrnetlab.run_command(["ip", "link", "set", f"{self.if_list[i]}", "up"])

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 1000:
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
                self.logger.info(f"Startup for {self.name} complete in: {startup_time}")
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


class WR_ctm(WR_base):
    def __init__(self, hostname, username, password, conn_mode, num, housing_id, location_id):
        # NOTE: Can not use logger until superclass constructor is complete
        super(WR_ctm, self).__init__(
            hostname=hostname,
            username=username,
            password=password,
            conn_mode=conn_mode,
            num=num,
            housing_id=housing_id,
            location_id=location_id,
            variant="wr-ctm",
            product_number="ne26xqsfp28",
            size="10737418240",
            num_backplane_if=5,
            ram=10240,
            num_nics=0,
            smp="4,sockets=1,dies=1,cores=2,threads=2"
        )

        # 179 - BGP
        # 225 - debug shell
        # 9340 - gRIBI
        # 9559 - P4RT
        # 10161 - gNMI/gNOI alternate
        self.mgmt_tcp_ports.extend([179, 225, 9340, 9559, 10161])

        vrnetlab.run_command(["cp", "/CTM_ap.img", f"/{self.name}_ap.img"])
        self.qemu_args.extend(
            [
                "-drive",
                f"if=none,file=/{self.name}_ap.img,format=raw,id=disk2",
                "-device",
                "ide-hd,bus=ide.1,drive=disk2,id=sata0-0-1",
            ]
        )

    def gen_mgmt(self):
        """Generate mgmt interface(s)

        We override the default function for the wr-ctm
        """
        res = []

        if self.housing_id == "1":
            # debug interface
            res = super(WR_ctm, self).gen_mgmt()
            replace_index = res.index("virtio-net-pci,netdev=p00,mac=%s" % self.mgmt_mac)
            res[replace_index] += ",multifunction=on,addr=0x3"

        else:
            # debug interface
            res.extend(["-device",
                        "virtio-net-pci,netdev=p00,mac=%s,multifunction=on,addr=0x3" % self.mgmt_mac,
                        "-netdev",
                        "user,id=p00,net=10.0.0.0/24,host=10.0.0.2,dns=10.0.0.3,dhcpstart=10.0.0.%02x" % (IPV4_ADDR_OCTET4_BASE + self.num)])

        # mgmt interface
        res.extend(["-device",
                    "virtio-net-pci,netdev=p01,mac=%s,bus=pcie.0,addr=0x3.0x1" % (self.mgmt_mac[:-1] + "1"),
                    "-netdev",
                    f"tap,ifname=wr-mgmt-{self.housing_id}-{self.location_id},id=p01,script=no,downscript=no"])

        # add virtio NIC for internal control plane interface to wr-ctm
        res.extend(["-device",
                    "virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,multifunction=on,addr=0x4"
                    % (self.if_list[0], gen_shared_mac(int(self.housing_id), int(self.location_id), 2, 0)),
                    "-netdev",
                    "tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[0], self.if_list[0])])

        for i in range(1, self.num_backplane_if):
            res.extend(["-device",
                        "virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,addr=0x4.0x%s"
                        % (self.if_list[i], gen_shared_mac(int(self.housing_id), int(self.location_id), i+2, i), i),
                        "-netdev",
                        "tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[i], self.if_list[i])])

        return res


class WR_qb(WR_base):
    def __init__(self, hostname, username, password, conn_mode, num, housing_id, location_id, nic_eth_start):
        # NOTE: Can not use logger until superclass constructor is complete
        super(WR_qb, self).__init__(
            hostname=hostname,
            username=username,
            password=password,
            conn_mode=conn_mode,
            num=num,
            housing_id=housing_id,
            location_id=location_id,
            variant="wr-qb",
            product_number="qb615xqsfpdd",
            size="5368709120",
            num_backplane_if=6,
            ram=5120,
            num_nics=15,
            smp="4,sockets=1,dies=1,cores=2,threads=2"
        )
        self.start_nic_eth_idx = nic_eth_start

    def gen_mgmt(self):
        """Generate mgmt interface(s)

        We override the default function for the wr-qb
        """
        res = []

        # debug interface
        res.extend(["-device",
                    "virtio-net-pci,netdev=p00,mac=%s,multifunction=on,addr=0x3" % self.mgmt_mac,
                    "-netdev",
                    "user,id=p00,net=10.0.0.0/24,host=10.0.0.2,dns=10.0.0.3,dhcpstart=10.0.0.%02x" % (IPV4_ADDR_OCTET4_BASE + self.num)])

        # add virtio NIC for internal control plane interface to wr-qb
        res.extend(["-device",
                    "virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,multifunction=on,addr=0x4"
                    % (self.if_list[0], gen_shared_mac(int(self.housing_id), int(self.location_id), 2, 0)),
                    "-netdev",
                    "tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[0], self.if_list[0])])

        for i in range(1, self.num_backplane_if):
            res.extend(["-device",
                        "virtio-net-pci,netdev=%s,mac=%s,bus=pcie.0,addr=0x4.0x%s"
                        % (self.if_list[i], gen_shared_mac(int(self.housing_id),  int(self.location_id), i+2, i), i),
                        "-netdev",
                        "tap,ifname=%s,id=%s,script=no,downscript=no" % (self.if_list[i], self.if_list[i])])

        return res

    def gen_nics(self):
        """Generate qemu args for the normal traffic carrying interface(s)"""
        self.nic_provision_delay()

        res = []

        if self.conn_mode == "tc":
            self.create_tc_tap_ifup()

        start_eth = self.start_nic_eth_idx
        end_eth = self.start_nic_eth_idx + self.num_nics
        addr = 0x8
        ext = 0x0
        for i in range(start_eth, end_eth):

            if ext == 0:
                mf = "multifunction=on,"
                ext_str = ""
            else:
                mf = ""
                ext_str = f".0x{ext}"

            mac = f"{self.mgmt_mac[:-2]}{(i+0x10):02x}"

            res.extend([
                "-device",
                f"{self.nic_type},netdev=p{i:02d},mac={mac},bus=pcie.0,{mf}addr=0x{addr:02x}{ext_str}"
            ])

            # if the matching container interface ethX doesn't exist, create a dummy interface
            if not os.path.exists(f"/sys/class/net/eth{i}"):
                res.extend(["-netdev", f"socket,id=p{i:02d},listen=:{i + 10000}"])
            else:
                res.extend(["-netdev", f"tap,id=p{i:02d},ifname=tap{i},script=/etc/tc-tap-ifup,downscript=no"])

            # increment bus addressing
            ext = (ext + 1) % 8
            if ext == 0:
                addr += 1

        return res


class WR(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)

        setup_json = "/setup.json"
        if not os.path.exists(setup_json):
            raise Exception(f"Json file {setup_json} not found")
        self.logger.info(f"Json file {setup_json} exists")

        with open(setup_json) as file:
            vm_info = json.load(file)

        self.logger.info(f"Unzip {OVMF_VARS_gz}...")
        if not os.path.exists(OVMF_VARS_gz):
            raise Exception(f"File {OVMF_VARS_gz} not found")
        vrnetlab.run_command(["gunzip", OVMF_VARS_gz])

        self.logger.info(f"Unzip {CTM_AP_gz} ...")
        if not os.path.exists(CTM_AP_gz):
            raise Exception(f"File {CTM_AP_gz} not found")
        vrnetlab.run_command(["gunzip", CTM_AP_gz])

        self.logger.info(f"Extracting {CTM_AP}...")
        if not os.path.exists(CTM_AP):
            raise Exception(f"File {CTM_AP} not found")
        vrnetlab.run_command(["tar", "xSf", CTM_AP])

        vrnetlab.run_command(["mkdir", "/instance_disk"])

        self.vms = []

        # add more later
        vm_class = {
            "wr-ctm": WR_ctm,
            "wr-qbox": WR_qb,
        }

        self.logger.debug(f"Number of nodes: {len(vm_info)}")
        if len(vm_info) > 1:
            raise Exception(f"{setup_json} is invalid. Maximum number of node is 1")

        for wr in vm_info:
            for housing_id in vm_info[wr]:
                global HOUSING_POOL_MAX
                HOUSING_POOL_MAX = max(HOUSING_POOL_MAX, housing_id)

        num = 0
        start_eth = 1

        for wr in vm_info:
            self.logger.info(f"WR: {wr}")
            node_dict = vm_info[wr]

            for housing_id in node_dict:
                housing_dict = node_dict[housing_id]
                housing_type = housing_dict.get("type", "wr13")

                for location_id in housing_dict:
                    if location_id == 'type':
                        continue

                    box_type = housing_dict[location_id]['type']
                    self.logger.info(f"----------------VM{num} INFO-----------------")
                    self.logger.info(f"housing: {housing_id}, housing type: {housing_type}, location: {location_id}, box_type: {box_type}")

                    args = [
                        hostname,
                        username,
                        password,
                        conn_mode,
                        num,
                        housing_id,
                        location_id
                    ]

                    if box_type == "wr-qbox":
                        args.append(start_eth)
                        vm = vm_class[box_type](*args)
                        start_eth += vm.num_nics
                    else:
                        vm = vm_class[box_type](*args)

                    self.vms.append(vm)
                    num += 1

        # set up bridge to connect vms
        self.logger.debug("Creating linux bridge...")
        vrnetlab.run_command(["brctl", "addbr", f"{LINUX_BRIDGE}"])
        vrnetlab.run_command(["ip", "link", "set", f"{LINUX_BRIDGE}", "up"])


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
