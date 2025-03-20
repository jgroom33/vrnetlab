# SAOS 10.x VM

This is the vrnetlab docker image for SAOS 10.x VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

Download a qcow2 backing file and place it in this directory (internal location: https://artifactory.ciena.com/valimar-snapshot/backing_file/).  This file must be named meta_\<VERSION\>-core-x86-64-disk.qcow2 for the make logic to find the version to use with the docker image.

Run `make`.

After typing `make`, a new image will appear named `vrnetlab/ciena_saos:<version>`.

Run `docker images` to confirm this.

## Variants

You must specify the saos variant in the topology.

- 3948
- 3984
- 3985
- 5130
- 5131
- 5132
- 5134
- 5144
- 5162
- 5164
- 5166
- 5168
- 5170
- 5171
- 8110
- 8112
- 8114
- 8140
- 8190
- 8192

## Usage

You can define the image easily and use it in a topolgy.

### Interface naming
- `eth0` - Node management interface
- `eth1` - First dataplane interface
- `ethX` - Subsequent dataplane interfaces will count onwards from 1. For example, the third dataplane interface will be `eth3`

### Example: Two or more nodes with links
```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    saos-1:
      kind: linux
      image: vrnetlab/vrnetlab/ciena_saos:<tag>
      type: <variant>
    saos-2:
      kind: linux
      image: vrnetlab/vrnetlab/ciena_saos:<tag>
      type: <variant>

  links:
    - endpoints: ["saos-1:eth1", "saos-2:eth1"]
    - endpoints: ["saos-1:eth2", "saos-2:eth2"]
```

## System requirements

- CPU: 2 cores
- RAM: 8GB
- DISK: ~40GB

## Configuration

Initial confiuration application is not yet supported.

## Limitations

* Serial numbers are fixed.
* The issue tracked by [PNVAL-227058](https://agile-jira.ciena.com/browse/PNVAL-227058) will apply.

## Contact

If you have issues, contact Dave Pelton (<dpelton@ciena.com>), but note that support for this image is currently a best-effort activity.
