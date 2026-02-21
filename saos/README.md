# vrnetlab / Ciena SAOS 10.x

This is the vrnetlab docker image for Ciena SAOS 10.x.

> Available with [containerlab](https://containerlab.dev) as [`linux`](https://containerlab.dev/manual/kinds/linux/) kind.

## Building the docker image

1. Obtain a SAOS qcow2 image from Ciena.
2. Place it in this directory (`vrnetlab/saos`).
3. Rename it to `meta_<VERSION>-core-x86-64-disk.qcow2`.
4. Run `make` (or `make docker-image`).

The resulting image is `vrnetlab/ciena_saos:<VERSION>`.

## Variants

Set the node `type` to one of these supported SAOS variants:

- `3948`
- `3984`
- `3985`
- `5130`
- `5131`
- `5132`
- `5134`
- `5144`
- `5162`
- `5164`
- `5166`
- `5168`
- `5169`
- `5170`
- `5171`
- `5184`
- `5186`
- `8110`
- `8112`
- `8114`
- `8140`
- `8190`
- `8192`

## Usage

### Console access

Serial console access is available via telnet on port `5000`:

```bash
telnet <container-name> 5000
```

### Interface naming

- `eth0`: management interface
- `eth1`: first dataplane interface
- `ethX`: remaining dataplane interfaces (for example, the third dataplane interface is `eth3`)

### Example topology

```yaml
name: mylab
topology:
  nodes:
    saos-1:
      kind: linux
      image: vrnetlab/ciena_saos:<tag>
      type: <variant>
    saos-2:
      kind: linux
      image: vrnetlab/ciena_saos:<tag>
      type: <variant>

  links:
    - endpoints: ["saos-1:eth1", "saos-2:eth1"]
    - endpoints: ["saos-1:eth2", "saos-2:eth2"]
```

## Startup partial configuration

Startup partial config is supported.

- Provide a file path with `SAOS_STARTUP_CONFIG_PATH`, or mount a file containing `.partial` in its name into `/config/`.
- CLI partial config is applied over SSH.
- XML partial config is applied over NETCONF.
- Retry interval is controlled by `SAOS_STARTUP_PARTIAL_RETRY_S` (default: `15` seconds).

## State tracking

SAOS startup progress is tracked in `/state.json` and in logs with `STATE ...` messages.

Startup state order:

- `waiting_for_login`: VM booted and waiting for a login prompt.
- `login_available`: login prompt detected.
- `password_revert`: login/password workflow completed and stable credentials are in use.
- `bootstrap_done`: SAOS bootstrap reports done.
- `config_ready`: config mode is reachable.
- `config_base_applied`: base management config applied.
- `ssh_ready`: management SSH probe succeeded.
- `startup_partial_applied`: partial startup config stage completed.
- `healthy`: startup pipeline complete.

Timeouts are evaluated per next expected state. If a timeout is hit, `timed_out_state` is set in `/state.json`, and health is reported as `unhealthy:timeout:<state>`.

### Monitoring state tracking

```bash
# Show current state and timeout info
docker exec <container-name> cat /state.json

# If jq is available, show a compact summary
docker exec <container-name> sh -lc 'cat /state.json | jq "{current, timed_out_state, timed_out_at, meta}"'

# Follow state transitions in logs
docker logs -f <container-name> | grep --line-buffered "STATE "

# Show the health status file used by the health check
docker exec <container-name> cat /health
```

Useful state/monitoring environment variables:

- `SAOS_STATE_PROBE_INTERVAL_S`
- `SAOS_PASSTHROUGH_SSH_GRACE_S`
- `SAOS_STATE_TIMEOUT_LOGIN_S`
- `SAOS_STATE_TIMEOUT_PASSWORD_REVERT_S`
- `SAOS_STATE_TIMEOUT_BOOTSTRAP_S`
- `SAOS_STATE_TIMEOUT_CONFIG_S`
- `SAOS_STATE_TIMEOUT_BASE_CONFIG_S`
- `SAOS_STATE_TIMEOUT_SSH_S`
- `SAOS_STATE_TIMEOUT_PARTIAL_S`
- `SAOS_STATE_TIMEOUT_HEALTHY_S`
- `SAOS_STATE_TIMEOUT_STARTUP_PARTIAL_PASSTHROUGH_S`
- `SAOS_STATE_TIMEOUT_BOOTSTRAP_PASSWORD_S`
- `SAOS_NEW_PASSWORD`
- `SAOS_PASSWORD_CMD_TIMEOUT_S`
- `SAOS_PASSWORD_VERIFY_TIMEOUT_S`

## System requirements

- CPU: 2 cores
- RAM: 8 GB
- Disk: ~40 GB

## Limitations

- Serial numbers are synthetic and auto-generated for simulation use.
