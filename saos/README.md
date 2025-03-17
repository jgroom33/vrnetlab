# SAOS 10.x VM

This is the vrnetlab docker image for SAOS 10.x VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

Download a qcow2 backing file and place it in this directory (internal location: https://artifactory.ciena.com/valimar-snapshot/backing_file/).  This file must be named meta_\<VERSION\>-core-x86-64-disk.qcow2 for the make logic to find the version to use with the docker image.

Run `make`.

After typing `make`, a new image will appear named `vrnetlab/ciena_saos:<version>`.

Run `docker images` to confirm this.

## System requirements

- CPU: 2 cores
- RAM: 8GB
- DISK: ~40GB

## Configuration

Initial confiuration application is not yet supported.

## Limitations

* The launch command is using a hard coded variant (5132).
* Serial numbers are fixed.
* The issue tracked by [PNVAL-227058](https://agile-jira.ciena.com/browse/PNVAL-227058) will apply.

## Contact

If you have issues, contact Dave Pelton (<dpelton@ciena.com>), but note that support for this image is currently a best-effort activity.
