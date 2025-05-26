# WR VM

This is the vrnetlab docker image for Waverouter VM based simulator.

> Available with [containerlab](https://containerlab.dev) as ['linux'](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

- Need a <wr-name>.qcow2
    - Start a WR-CTM with --no-reboot (using the simulator script, eg `sudo -E BRIDGE=virbr0 VM_NAME=WR1-CTM-1-7 NODE_NAME=WR1 HOUSING_ID=1 HOUSING_POOL=2 LOCATION_ID=7 BOXLANBRIDGE=virbrWR1 IS_DUAL_CTM=no OFLD_SLOTS=0 ./wr_ctm_sim.sh --version wr-80-00-00-0081 --wr-type wr13 --debug --ubridge --no-reboot`)
    - Convert the <wr-name>-disk.img file into qcow2 (`qemu-img convert -f raw -O qcow2 <wr-name>-disk.img <wr-name>-disk.qcow2`)
- Edit the json file
    - Edit the provided example json file to define the waverouter node (Format: similar to the json files in /setup_files folder in the waverouter-simulation repo minus the `"Devices": {}`)

### Example json file
```json
# 1-1c1q.json
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


Run `make VERSION=<version>`. (eg VERSION=wr-80-00-00-0081)

After typing `make VERSION=<version>`, a new image will appear named `vrnetlab/ciena_waverouter:<version>`.

Run `docker images` to confirm this.

## Usage

### Console access

Serial console access is available via telnet on port 500x (eg in the example json qbox was provided first so qbox serial is 5000, then ctm is 5001 and so on)
```
telnet <container-name> 5000
telnet <container-name> 5001
...
```

### Example topology file
```yaml
# topology.clab.yaml
name: mylab
topology:
  nodes:
    wr-1:
      kind: linux
      image: vrnetlab/ciena_waverouter:wr-80-00-00-0081
      binds:
        - ./<name>.json:/setup.json

```

## System requirements

- CPU: 4 cores
- RAM: 10GB
- DISK: ~2GB

## Configuration

Initial confiuration application is not yet supported.

## Contact

If you have issues, contact Dave Pelton (<dpelton@ciena.com>), but note that support for this image is currently a best-effort activity.
