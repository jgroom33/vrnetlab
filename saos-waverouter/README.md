# WR VM

This is the vrnetlab docker image for Waverouter VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

- Need a <wr-name>.qcow2
    - Start a WR-CTM with --no-start (using the simulator script)
    - Convert the <wr-name>-disk.img file into qcow2 (`qemu-img convert -f raw -O qcow2 <wr-name>-disk.img <wr-name>-disk.qcow2`)
- Need a <wr-name>-disk_ap.img.tar inside docker/
    - NOTES: For now make sure before tar and after untar the file is named WR1-CTM_ap.img
    - Start a WR-CTM with --no-start
    - Rename the <wr-name>-disk_ap.img to WR1-CTM_ap.img (Need to fix this hard coded naming)
    - Tar the WR1-CTM_ap.img (`tar cSf WR1-CTM_ap.img.tar WR1-CTM_ap.img`)
- Need OVMF_VARS_bkup.fd.gz inside docker/
- Need qbox-disk/WR1-QBox-1-5-disk.qcow2 inside docker/
    - cd to docker/
    - mkdir qbox-disk/
    - Start a WR-QB with --no-start (HOUSING_ID=1, LOCATION_ID=5)
    - Convert the <wr-name>-disk.img file into qcow2 (`qemu-img convert -f raw -O qcow2 <wr-name>-disk.img <wr-name>-disk.qcow2`)
    - mv the qcow2 into docker/qbox-disk/

Run `make`.

After typing `make`, a new image will appear named `vrnetlab/ciena_waverouter:<version>`.

Run `docker images` to confirm this.

## Usage

### Console access

Serial console access is available via telnet on port 5000 of the container for wr-ctm, on port 5001 of the container for wr-qb:
```
telnet <container-name> 5000
telnet <container-name> 5001
```

### Example topology file
```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    wr-1:
      kind: linux
      image: vrnetlab/ciena_waverouter:wr-10-09-02-0572
```

## System requirements

- CPU: 4 cores
- RAM: 10GB
- DISK: ~2GB

## Configuration

Initial confiuration application is not yet supported.

## Limitations

* Serial numbers are fixed.

## Contact

If you have issues, contact Dave Pelton (<dpelton@ciena.com>), but note that support for this image is currently a best-effort activity.
