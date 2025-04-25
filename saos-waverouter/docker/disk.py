import shutil
import contextlib
import errno
import subprocess
import vrnetlab
import os
import logging
import sys
from collections import namedtuple

PartitionInfo = namedtuple(
    'PartitionInfo', ['num', 'label', 'size', 'typecode', 'mkfs_type', 'instance_data_files']
)

logger = logging.getLogger()


# https://serverfault.com/questions/896854/how-can-i-give-access-to-loopback-devices-which-are-created-dynamically-to-a-doc
# This script helps partition created dynamically visible to the container
def run_partition_helper():
    helper_script = "./partition_helper.sh"
    logger.info(f"Running {helper_script} script")
    try:
        subprocess.run(
            ["sh", helper_script],
            stderr=subprocess.PIPE,
            check=True
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Stderr: {e.returncode}")
    except FileNotFoundError:
        logger.error(f"Exception: {helper_script} not found")


@contextlib.contextmanager
def losetup(disk_file):
    loop_device = None
    try:
        logger.info("Loop mounting %s" % disk_file)
        losetup_cmd = ["losetup", "-f", "-P", "--show", disk_file]

        logger.debug("Entering losetup context: %s" % ' '.join(losetup_cmd))

        losetup_cmd_res = vrnetlab.run_command(losetup_cmd)
        run_partition_helper()
        if losetup_cmd_res is None:
            raise Exception("Losetup failed %d" % losetup_cmd_res[1])

        loop_device = losetup_cmd_res[0].decode().rstrip("\n\r")

        if not os.path.exists(loop_device + "p1"):
            raise Exception("Failure to create %s" % (loop_device + "p1"))
        else:
            logger.info(f"{loop_device}p1 exists")

        logger.info("Losetup device: %s" % loop_device)
        yield loop_device
    finally:
        if loop_device is not None and os.path.exists(loop_device) and os.path.isdir(f'/sys/block/{os.path.basename(loop_device)}/loop'):
            logger.info("Releasing losetup device: %s" % loop_device)
            detach_cmd = ['losetup', '--detach', loop_device]
            logger.debug("Detach command: %s" % ' '.join(detach_cmd))
            vrnetlab.run_command(detach_cmd)


def create_partition(size, name, number, typecode, device):
    typecode = format(typecode, 'x')
    try:
        logger.info("Creating partition %s" % name)
        sgdisk_cmd = ['sgdisk', '--new=%s::+%s' % (number, size),
                      '--typecode=%s:%s' % (number, typecode),
                      '--change-name=%s:%s' % (number, name),
                      device]
        logger.debug("Sgdisk command: %s" % ' '.join(sgdisk_cmd))
        vrnetlab.run_command(sgdisk_cmd)

    except BaseException:
        logger.error("Exception: unable to create partition %s" % name)
        raise


@contextlib.contextmanager
def loop_mount(filename, mount_path):
    try:
        mounted = False
        mkdir_p(mount_path)
        mount_cmd = ['mount', filename, mount_path]
        logger.info("Mounting %s to %s" % (filename, mount_path))
        logger.debug("Mount command: %s" % ' '.join(mount_cmd))
        vrnetlab.run_command(mount_cmd)
        mounted = True
        yield mount_path
    finally:
        if mounted:
            unmount_cmd = ['umount', mount_path]
            logger.info("Umounting %s at %s" % (filename, mount_path))
            logger.debug("Unmount command: %s" % ' '.join(unmount_cmd))
            vrnetlab.run_command(unmount_cmd)


def create_disk_image(path, size, partitions, guest_name):
    try:
        fallocate_cmd = ["fallocate", "-l", size, path]
        logger.info("Fallocating: %s" % ' '.join(fallocate_cmd))
        fallocate_cmd_res = vrnetlab.run_command(fallocate_cmd)

        if fallocate_cmd_res is None:
            if os.path.isfile(path):
                os.remove(path)
            logger.error("Failed to allocate", size, "for", path)
            logger.error("Check that the target path has sufficient space, and verify that the target is not hosted by a network mounted filesystem.", fallocate_cmd_res[1])
            sys.exit(1)

        # Partition disk image and then setup loop device
        for part in partitions:
            create_partition(part.size, part.label, part.num, part.typecode, path)
        with losetup(path) as loop_device:
            for part in partitions:
                partdev = loop_device + "p" + str(part.num)

                mkfs_cmd = ['mkfs.ext4', '-F', '-q', '-L', f"{part.label}", f"{partdev}"]
                logger.debug("mkfs.ext4 command: %s" % ' '.join(mkfs_cmd))
                vrnetlab.run_command(mkfs_cmd)

                install_partition(partdev, part, guest_name.upper(), part.instance_data_files)

                tune_cmd = ['tune2fs', '-O^metadata_csum', '-i0', f"{partdev}"]
                logger.debug("tune command: %s" % ' '.join(tune_cmd))
                vrnetlab.run_command(tune_cmd)
    except BaseException:
        try:
            logger.info("Removing disk image", path)
            os.remove(path)
        except FileNotFoundError:
            pass
        raise


# http://stackoverflow.com/questions/600268/mkdir-p-functionality-in-python
def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def get_mountpoints(dev: str) -> list:
    mountpoints = []
    try:
        with open('/proc/mounts', 'r') as mountsfile:
            for line in mountsfile.readlines():
                (device, mount_point, _) = line.split(' ', 2)

                if device and mount_point and device == dev:
                    mountpoints.append(mount_point)
    except OSError:
        logger.error(f"Cannot parse /proc/mounts to identify mount points for {dev}")
    return mountpoints


def install_partition(partdev, partition, guest_name, instance_data_files):
    try:
        stage_path = "./{}-staging".format(guest_name)
        mount_path = os.path.join(stage_path, partition.label)
        mkdir_p(mount_path)

        with loop_mount(partdev, mount_path):
            for file in instance_data_files:
                if not os.path.exists(file):
                    raise Exception(f"File {file} not found")
                if not os.path.exists(mount_path):
                    raise Exception(f"File {mount_path} not found")

                cp_json_command = ["cp", "-R", f"{file}", f"{mount_path}/"]
                logger.debug(f"Copy {file} command: %s" % ' '.join(cp_json_command))
                vrnetlab.run_command(cp_json_command)

                # Flush all disk changes
                os.sync()

        # Check if partition get mounted to any other device, could be a case where windows/gnome disk manager conflicting with loop device mount
        mountpoints = get_mountpoints(partdev)
        if mountpoints:
            try:
                mount_point_cmd = ['umount', '--all-targets', '--recursive', partdev]
                logger.debug("Mount points command: %s" % ' '.join(mount_point_cmd))
                vrnetlab.run_command(mount_point_cmd)
            # If all-targets umount failed try by umounting individual mount point
            except (OSError, subprocess.CalledProcessError):
                for mountpoint in mountpoints:
                    umount_point_cmd = ['umount', mountpoint]
                    logger.debug("Umount points command: %s" % ' '.join(umount_point_cmd))
                    vrnetlab.run_command(umount_point_cmd)
    finally:
        shutil.rmtree(stage_path)
