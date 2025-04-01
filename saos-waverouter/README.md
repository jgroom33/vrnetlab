# WR VM (this README needs to be updated - for now it is for WR-CTM)

This is the vrnetlab docker image for WR-CTM VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

- Need OVMF_CODE-pure-efi.fd inside docker/
- Need a qcow2 file
    - Start a WR-CTM with --no-start 
    - Convert the img file into qcow2

Run `make`.

After typing `make`, a new image will appear named `vrnetlab/ciena_wr-ctm:<version>`.

Run `docker images` to confirm this.

## Variants

You must specify the waverouter variant in the topology.

- wr-ctm
- wr-fb
- wr-qb
- wr-ub

## Usage

### Console access

Serial console access is available via telnet on port 5000 of the container:
```
telnet <container-name> 5000
```

### Example topology file
```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    wr-ctm-1:
      kind: linux
      image: vrnetlab/ciena_wr-ctm:wr-10-09-02-0572
      type: wr-ctm
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
