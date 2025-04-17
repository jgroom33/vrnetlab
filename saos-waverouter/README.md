# WR VM

This is the vrnetlab docker image for Waverouter VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

- Need a <wr-name>.qcow2
    - Start a WR-CTM with --no-start (using the simulator script, eg `sudo -E BRIDGE=virbr0 VM_NAME=WR1-CTM-1-7 NODE_NAME=WR1 HOUSING_ID=1 HOUSING_POOL=1 LOCATION_ID=7 BOXLANBRIDGE=virbrWR1 IS_DUAL_CTM=no OFLD_SLOTS=0 ./wr_ctm_sim.sh --version wr-10-09-02-0579 --wr-type wr13 --debug --ubridge --no-start`)
    - Convert the <wr-name>-disk.img file into qcow2 (`qemu-img convert -f raw -O qcow2 <wr-name>-disk.img <wr-name>-disk.qcow2`)

Run `make VERSION=<version>`. (eg VERSION=wr-10-09-02-0579)

After typing `make VERSION=<version>`, a new image will appear named `vrnetlab/ciena_waverouter:<version>`.

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
