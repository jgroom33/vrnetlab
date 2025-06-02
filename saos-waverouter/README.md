# WR VM

This is the vrnetlab docker image for Waverouter VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

Generate a disk image:
  - git clone the [sim_scripts](https://bitbucket.ciena.com/projects/EVERNIGHT/repos/sim_scripts/browse) repo.  Ensure that this clone is not in a network mounted location (i.e. do not use your Linux home directory).
  - clean previous WR-BLOB instance if it exists.  From the sim_scripts directory:<br>
    `sudo ./sim_clean.sh WR-BLOB`<br>
    ignore errors from this command - it will complain if the simulator isn't running.
  - Using the wr_ctm_sim.sh script in the sim_scripts checkout, launch a WR simulator using the --no-reboot option:<br>
    `sudo -E VM_NAME=WR-BLOB NODE_NAME=WR_NOPE HOUSING_ID=1 HOUSING_POOL=1 LOCATION_ID=7 BOXLANBRIDGE=virbr0 ./wr_ctm_sim.sh --version wr-80-00-00-0124 --debug --no-reboot`<br>
    Notes:
    - an up to date checkout of the sim_scripts repo is required to use the --no-reboot option.
    - this command will create a simulator instance that will exit once ONIE has run and the disk image is ready.
    - if you see an error message `ERROR     WR-BLOB is already running`, then run the ./sim_clean.sh command above.
  - Convert the raw disk image file into qcow2:<br>
    `qemu-img convert -f raw -O qcow2 WR-BLOB-disk.img WR-BLOB-disk.qcow2`

Copy the qcow2 disk image into this directory.

Run `make VERSION=<version>`. (eg VERSION=wr-80-00-00-0124)

After typing `make VERSION=<version>`, a new image will appear named `vrnetlab/ciena_waverouter:<version>`.

Run `docker images` to confirm this.

## Usage

### Waverouter system components
A JSON file is used to define the Waverouter components in the simulator.  This file must be referenced in the "binds" section of the containerlab YAML file.  This file uses a format similar to the json files in /setup_files folder in the waverouter-simulation repo, but without the `"Devices": {}` delimiter.

#### Example json file
```json
{  
    "WR1": {
        "1": {
            "5": {
                "type": "wr-qbox"
            },
            "7": {
                "type": "wr-ctm"
            }
        }
    }
}
```

### Example topology file
```yaml
name: mylab
topology:
  nodes:
    wr-1:
      kind: linux
      image: vrnetlab/ciena_waverouter:wr-80-00-00-0124
      binds:
        - ./<name>.json:/setup.json
```

### Console access

Serial console access is available via telnet staring from port 5000.  The telnet port is based on the order the cards appear in the JSON file (eg in the example json qbox was provided first so qbox serial is 5000, then ctm is 5001 and so on)
```
telnet <container-name> 5000
telnet <container-name> 5001
...
```

## System requirements

For each card
- CPU: 4 cores for CTMs, 2 cores for other cards
- RAM: 10GB for CTMs, 5GB for other cards
- DISK: ~8GB baseline + per card use

## Configuration

Providing an initial configuration is not yet supported.

## Contact

If you have issues, contact Dave Pelton (<dpelton@ciena.com>), but note that support for this image is currently a best-effort activity.
