#!/usr/bin/env python3

import datetime
import json
import time
import logging
import os
import re
import signal
import sys
import vrnetlab
import uuid
import socket
import resource
import subprocess
import asyncio
from telnet_logger import start_telnet_loggers
proxy_port = 5000


def handle_SIGCHLD(_signal, _frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(_signal, _frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

SOFT_ULIMIT_NOFILE = 524288  # from containerlab ulimit on Oracle linux 10
TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
BOOTSTRAP_DONE_RE = re.compile(r"\bcore\b.*Bootstrap Done", re.IGNORECASE)
PROMPT_PATTERNS = [
    re.compile(rb"BASE-dnx-SIM\??[>#]"),
    re.compile(rb"[A-Za-z0-9_.@:()~/\-]+\$"),
    re.compile(rb"[A-Za-z0-9_.@:()~/\-]+\??[>#]"),
]
PROMPT_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@:()~/\-]+\??[>#]|[A-Za-z0-9_.@:()~/\-]+\$")
STATE_ORDER = [
    "waiting_for_login",
    "login_available",
    "bootstrap_done",
    "config_ready",
    "config_base_applied",
    "ssh_ready",
    "startup_partial_applied",
    "healthy",
]
DEFAULT_TIMEOUTS = {
    "login_available": 300,
    "bootstrap_done": 120,
    "config_ready": 300,
    "config_base_applied": 300,
    "ssh_ready": 1200,
    "startup_partial_applied": 300,
    "healthy": 30,
}
DEFAULT_PROBE_INTERVAL_S = 5
DEFAULT_PASSTHROUGH_SSH_GRACE_S = 60
DEFAULT_STARTUP_PARTIAL_RETRY_S = 15


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class SAOSStateTracker:
    def __init__(
        self,
        logger,
        state_order,
        timeouts,
        state_file="/state.json",
        start_time=None,
        meta=None,
    ):
        self.logger = logger
        self.state_order = state_order
        self.timeouts = timeouts
        self.state_file = state_file
        self.start_time = start_time
        self.states = []
        self.current = None
        self.timed_out_state = None
        self.timed_out_at = None
        self.meta = meta or {}

    def set_state(self, name):
        if self.current == name:
            return False
        ts = datetime.datetime.now()
        if self.start_time is None:
            self.start_time = ts
        self.current = name
        self.states.append({"name": name, "ts": ts})
        self._log_state(name, ts)
        self._write_state()
        return True

    def check_timeout(self):
        if self.timed_out_state:
            return self.timed_out_state
        if not self.current or self.current not in self.state_order:
            return None
        idx = self.state_order.index(self.current)
        if idx >= len(self.state_order) - 1:
            return None
        next_state = self.state_order[idx + 1]
        timeout_s = self.timeouts.get(next_state)
        if not timeout_s or timeout_s <= 0:
            return None
        prev_ts = self._state_ts(self.current)
        if prev_ts is None:
            return None
        now = datetime.datetime.now()
        if (now - prev_ts).total_seconds() > timeout_s:
            self.timed_out_state = next_state
            self.timed_out_at = now
            self.logger.error("STATE TIMEOUT %s after %ss", next_state, timeout_s)
            self._write_state()
            return next_state
        return None

    def _state_ts(self, name):
        for entry in self.states:
            if entry["name"] == name:
                return entry["ts"]
        return None

    def _log_state(self, name, ts):
        delta_s = None
        if self.start_time:
            delta_s = (ts - self.start_time).total_seconds()
        if delta_s is None:
            self.logger.info("STATE %s ts=%s", name, ts.isoformat())
        else:
            self.logger.info("STATE %s ts=%s delta_s=%.3f", name, ts.isoformat(), delta_s)

    def _write_state(self):
        payload = {
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "states": [
                {"name": entry["name"], "ts": entry["ts"].isoformat()}
                for entry in self.states
            ],
            "current": self.current,
            "timeouts_s": self.timeouts,
            "timed_out_state": self.timed_out_state,
            "timed_out_at": self.timed_out_at.isoformat() if self.timed_out_at else None,
            "meta": self.meta,
        }
        try:
            with open(self.state_file, "w") as fh:
                json.dump(payload, fh, indent=2)
        except Exception as exc:
            self.logger.debug("Failed to write state file: %s", exc)


class SAOS_vm(vrnetlab.VM):

    variant_map = {
        "3948": {
            "interface_count"       : 16,
        },
        "3984": {
            "interface_count"       : 6,
        },
        "3985": {
            "interface_count"       : 6,
        },
        "5130": {
            "interface_count"       : 14,
        },
        "5131": {
            "interface_count"       : 14,
        },
        "5132": {
            "interface_count"       : 4,
        },
        "5134": {
            "interface_count"       : 26,
        },
        "5144": {
            "interface_count"       : 28,
        },
        "5162": {
            "interface_count"       : 42,
        },
        "5164": {
            "interface_count"       : 36,
        },
        "5166": {
            "interface_count"       : 34,
        },
        "5168": {
            "interface_count"       : 36,
        },
        "5169": {
            "interface_count"       : 16,
        },
        "5170": {
            "interface_count"       : 44,
        },
        "5171": {
            "interface_count"       : 56,
        },
        "5184": {
            "interface_count"       : 36,
        },
        "5186": {
            "interface_count"       : 12,
        },
        "8110": {
            "interface_count"       : 58,
        },
        "8112": {
            "interface_count"       : 40,
        },
        "8114": {
            "interface_count"       : 78,
        },
        "8140": {
            "interface_count"       : 48,  # no CPU ports
        },
        "8190": {
            "interface_count"       : 36,  # no CPU ports
        },
        "8192": {
            "interface_count"       : 36,  # no CPU ports
        },
    }

    def __init__(self, hostname, username, password, conn_mode, state_tracker=None):
        disk_image = "/"
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
                break

        self.variant = os.environ.get("CLAB_LABEL_CLAB_NODE_TYPE")

        if self.variant is None:
            raise Exception("Missing saos variant in the yml file.")

        self.variant_data = SAOS_vm.variant_map.get(self.variant)
        if self.variant_data is None:
            raise Exception(f"Unsupported variant: {self.variant}")

        # NOTE: Can not use logger until superclass constructor is complete
        super(SAOS_vm, self).__init__(
            username, password, disk_image=disk_image, ram=8196, cpu="host", smp="2,sockets=1,cores=1,threads=2",
        )

        for i, arg in enumerate(self.qemu_args):
            if arg.startswith("telnet:0.0.0.0:50"):
                qemu_port = arg.replace("telnet:0.0.0.0:50", "telnet:0.0.0.0:51")
                self.logger.info(f"Overriding QEMU telnet port to {qemu_port}")
                self.qemu_args[i] = qemu_port
                break

        self.logger.info(f"Variant: {self.variant}")

        # 179 - BGP
        # 225 - debug shell
        # 4243 - docker daemon
        self.mgmt_tcp_ports.extend([179, 225, 4243])

        self.hostname = hostname
        self.state_tracker = state_tracker

        self.uuid = str(uuid.uuid4())
        self.serial_number = f"SIM{self.uuid}"[0:8]  # create an 8 character serial number

        self.nic_type = "virtio-net-pci"
        self.conn_mode = conn_mode
        self.num_nics = self.variant_data["interface_count"]

        self.qemu_args.extend(
            [
                "-name",
                f"{self.hostname}",
                "-machine",
                "q35,accel=kvm,dump-guest-core=off,smm=off",
                "-boot",
                "order=c",
                "-bios",
                "/usr/share/OVMF/OVMF_CODE.fd",
                "-uuid",
                f"{self.uuid}",
                "-net",
                "none",
            ]
        )
        self.smbios = [
            f"type=1,manufacturer=Ciena,product=CN{self.variant},serial={self.serial_number}",
            f"type=2,manufacturer=Ciena,product=CN{self.variant}",
            f"type=11,value=hostname:clab,value=mgmtMac:0,value=vmname:clab-{self.hostname}," +
            f"value=is-sim:true,value=locationId:0,value=jsonData:{{\\\"variant\\\":\\\"CN{self.variant}\\\"}}",
        ]
        self.hostname = hostname

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.state_tracker and self.state_tracker.timed_out_state:
            return

        if self.state_tracker and self.state_tracker.current is None and self.start_time:
            self.state_tracker.start_time = self.start_time
            self.state_tracker.set_state("waiting_for_login")

        if self.spins > 300:
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
                if self.state_tracker:
                    self.state_tracker.set_state("login_available")
                self.wait_write("diag", wait=None)
                self.wait_write("ciena123", wait="Password:")
                if self._wait_for_prompt(timeout=60, send_newline=True) is None:
                    self.logger.warning("login did not reach a prompt")
                    return
                self.logger.debug("login complete")

                if not self.wait_for_bootstrap_done():
                    self.logger.warning("bootstrap did not complete")
                    return
                if not self.apply_base_config():
                    self.logger.warning("base configuration did not complete")
                    return
                self.startup_config()
                # close telnet connection
                self.tn.close()
                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info(f"Startup complete in: {startup_time}")
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

    def _bootstrap_done_in_output(self, output):
        if not output:
            return False
        return BOOTSTRAP_DONE_RE.search(output) is not None

    def _mark_bootstrap_done(self):
        if self.state_tracker:
            self.state_tracker.set_state("bootstrap_done")

    def _state_timed_out(self):
        if not self.state_tracker:
            return False
        return self.state_tracker.check_timeout() is not None

    def wait_for_bootstrap_done(self):
        while True:
            if self._state_timed_out():
                return False
            op = self._send_cmd_wait("show bootstrap-status", timeout=60)
            if self._bootstrap_done_in_output(op):
                self._mark_bootstrap_done()
                return True
            time.sleep(5)

    def _wait_for_login_prompt(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            idx, match, _ = self.tn.expect([b"login:"], timeout=min(5, end - time.time()))
            if match and idx == 0:
                return True
            try:
                self.tn.write(b"\r")
            except Exception:
                pass
        return False

    def _wait_for_password_prompt(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            idx, match, _ = self.tn.expect([b"Password:"], timeout=min(5, end - time.time()))
            if match and idx == 0:
                return True
            try:
                self.tn.write(b"\r")
            except Exception:
                pass
        return False

    def _relogin(self, password="ciena123", timeout=60):
        if not self._wait_for_login_prompt(timeout=timeout):
            try:
                self.tn.write(b"exit\r")
            except Exception:
                pass
            if not self._wait_for_login_prompt(timeout=timeout):
                return False

        self.logger.debug("trying to log in with 'diag'")
        try:
            self.tn.write(b"diag\r")
        except Exception:
            return False
        if not self._wait_for_password_prompt(timeout=30):
            return False
        try:
            self.tn.write(f"{password}\r".encode())
        except Exception:
            return False
        self.logger.debug("login complete")
        return self._wait_for_prompt(timeout=60, send_newline=True) is not None

    def _enter_config_mode(self):
        timeout_s = DEFAULT_TIMEOUTS["config_ready"]
        if self.state_tracker:
            timeout_s = self.state_tracker.timeouts.get("config_ready", timeout_s)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._state_timed_out():
                return None
            op = self._send_cmd_wait("config", timeout=30, settle_s=2)
            if op is None:
                if not self._relogin(timeout=30):
                    return None
                continue
            if self._prompt_is_config(op):
                return op
            if (
                "SHELL PARSER FAILURE" in op
                or "no matching entry found" in op.lower()
                or self._prompt_is_sim(op)
            ):
                self.logger.debug(
                    "config not available in base CLI; attempting relogin for prompt transition"
                )
                if not self._relogin(timeout=30):
                    return None
                self._wait_for_non_sim_prompt(timeout=30)
                continue
            time.sleep(5)
        return None

    def apply_base_config(self):
        """Apply base config for hostname and management passthrough (if enabled)."""
        new_password = False
        ipv4 = None
        subnet = None
        if self.mgmt_passthrough and not self.mgmt_dhcp:
            ipv4, subnet = self.mgmt_address_ipv4.split("/")

        self.logger.info("applying base configuration")
        self._wait_for_prompt(timeout=60, send_newline=True)
        op = self._enter_config_mode()
        if self._state_timed_out():
            return False
        if op is None:
            self.logger.debug("config not ready, attempting re-login")
            if not self._relogin():
                self.logger.warning("unable to login for base config")
                return False
            op = self._enter_config_mode()
            if self._state_timed_out():
                return False
        if op is None:
            self.logger.warning("unable to enter config mode")
            return False
        # newer load has the string "password" in the output
        if op and "password" in op:
            new_password = True

        # newer load requires password change
        if new_password:
            self._send_cmd_wait(
                "system aaa authentication users user diag config password ciena1234",
                timeout=60,
            )
            if self._state_timed_out():
                return False
            if not self._relogin(password="ciena1234"):
                self.logger.warning("login failed after password change")
                return False
            op = self._enter_config_mode()
            if self._state_timed_out():
                return False
            if op is None:
                self.logger.warning("unable to enter config mode after password change")
                return False
            # change password back to default password
            self._send_cmd_wait(
                "system aaa authentication users user diag config password ciena123",
                timeout=60,
            )
            if self._state_timed_out():
                return False

        if self.state_tracker:
            self.state_tracker.set_state("config_ready")

        base_ok = True
        if self._send_cmd_wait(f"system config hostname {self.hostname}", timeout=60) is None:
            base_ok = False
        if self._state_timed_out():
            return False
        if ipv4 and subnet:
            if self._send_cmd_wait(
                "dhcp-client client mgmtbr0 admin-enable false", timeout=60
            ) is None:
                base_ok = False
            if self._state_timed_out():
                return False
            if self._send_cmd_wait(
                f"oc-if:interfaces interface mgmtbr0 ipv4 addresses address {ipv4} "
                f"config ip {ipv4} prefix-length {subnet}",
                timeout=60,
            ) is None:
                base_ok = False
            if self._state_timed_out():
                return False
            if self._send_cmd_wait(
                f"rib vrf default ipv4 0.0.0.0/0 next-hop {re.sub(r'\d+$', '1', ipv4)}",
                timeout=60,
            ) is None:
                base_ok = False
            if self._state_timed_out():
                return False
        if not self._exit_to_oper(timeout=30, max_exits=8):
            base_ok = False
        if not base_ok:
            self.logger.warning("base configuration did not complete")
            return False
        if self.state_tracker:
            self.state_tracker.set_state("config_base_applied")
        return True

    def startup_config(self):
        """Load additional config provided by user."""
        return

    def _wait_for_prompt(self, timeout=30, send_newline=False):
        if send_newline:
            try:
                self.tn.write(b"\r")
            except Exception:
                pass
        end = time.time() + timeout
        buffer = b""
        while time.time() < end:
            remaining = max(1, int(end - time.time()))
            idx, match, data = self.tn.expect(PROMPT_PATTERNS, timeout=min(5, remaining))
            if data:
                buffer += data
            if match:
                return buffer.decode(errors="ignore")
        return buffer.decode(errors="ignore") if buffer else None

    def _wait_for_prompt_settled(self, timeout=30, settle_s=1.0):
        end = time.time() + timeout
        buffer = b""
        matched = False
        while time.time() < end:
            remaining = max(1, int(end - time.time()))
            idx, match, data = self.tn.expect(PROMPT_PATTERNS, timeout=min(5, remaining))
            if data:
                buffer += data
            if match:
                matched = True
                settle_end = min(end, time.time() + settle_s)
                while time.time() < settle_end:
                    wait = max(0.1, min(0.5, settle_end - time.time()))
                    idx2, match2, data2 = self.tn.expect(PROMPT_PATTERNS, timeout=wait)
                    if data2:
                        buffer += data2
                        settle_end = min(end, time.time() + settle_s)
                    if match2:
                        settle_end = min(end, time.time() + settle_s)
                break
        if not matched:
            return None
        return buffer.decode(errors="ignore") if buffer else None

    def _last_prompt(self, output):
        if not output:
            return None
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line:
                continue
            last = None
            for match in PROMPT_TOKEN_RE.finditer(line):
                last = match.group(0)
            if last:
                return last
        return None

    def _prompt_is_config(self, output):
        prompt = self._last_prompt(output)
        return bool(prompt and prompt.endswith("#"))

    def _prompt_is_oper(self, output):
        prompt = self._last_prompt(output)
        if not prompt:
            return False
        return prompt.endswith(">") or prompt.endswith("$")

    def _prompt_is_sim(self, output):
        prompt = self._last_prompt(output)
        return bool(prompt and prompt.startswith("BASE-dnx-SIM"))

    def _wait_for_non_sim_prompt(self, timeout=120):
        end = time.time() + timeout
        last_prompt = None
        while time.time() < end:
            output = self._wait_for_prompt(timeout=10, send_newline=True)
            if output:
                prompt = self._last_prompt(output)
                if prompt and prompt != last_prompt:
                    self.logger.debug("observed prompt: %s", prompt)
                    last_prompt = prompt
                if prompt and not prompt.startswith("BASE-dnx-SIM"):
                    return True
            if self._state_timed_out():
                return False
            time.sleep(2)
        return False

    def _exit_to_oper(self, timeout=30, max_exits=8):
        for _ in range(max_exits):
            op = self._send_cmd_wait("exit", timeout=timeout, settle_s=2)
            if self._state_timed_out():
                return False
            if op and self._prompt_is_oper(op):
                return True
        op = self._wait_for_prompt(timeout=timeout, send_newline=True)
        if op and self._prompt_is_oper(op):
            return True
        return False

    def _send_cmd_wait(self, cmd, timeout=30, settle_s=0):
        self.logger.debug("writing to serial console: '%s'", cmd)
        try:
            self.tn.read_very_eager()
        except Exception:
            pass
        self.tn.write(f"{cmd}\r".encode())
        if settle_s > 0:
            output = self._wait_for_prompt_settled(timeout=timeout, settle_s=settle_s)
        else:
            output = self._wait_for_prompt(timeout=timeout, send_newline=False)
        if output is None:
            output = self._wait_for_prompt(timeout=10, send_newline=True)
        return output


class SAOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)
        self.health_mode = os.environ.get("SAOS_HEALTH_MODE", "strict").lower()
        if self.health_mode not in ("strict", "progressive"):
            self.logger.warning(
                "Invalid SAOS_HEALTH_MODE=%s, defaulting to strict",
                self.health_mode,
            )
            self.health_mode = "strict"

        self.state_timeouts = {
            "login_available": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_LOGIN_S", DEFAULT_TIMEOUTS["login_available"]
            ),
            "bootstrap_done": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_BOOTSTRAP_S", DEFAULT_TIMEOUTS["bootstrap_done"]
            ),
            "config_ready": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_CONFIG_S", DEFAULT_TIMEOUTS["config_ready"]
            ),
            "config_base_applied": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_BASE_CONFIG_S",
                DEFAULT_TIMEOUTS["config_base_applied"],
            ),
            "ssh_ready": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_SSH_S", DEFAULT_TIMEOUTS["ssh_ready"]
            ),
            "startup_partial_applied": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_PARTIAL_S",
                DEFAULT_TIMEOUTS["startup_partial_applied"],
            ),
            "healthy": self._read_timeout_env(
                "SAOS_STATE_TIMEOUT_HEALTHY_S", DEFAULT_TIMEOUTS["healthy"]
            ),
        }
        self.state_tracker = SAOSStateTracker(
            self.logger,
            state_order=STATE_ORDER,
            timeouts=self.state_timeouts,
            state_file="/state.json",
            meta={"health_mode": self.health_mode},
        )
        self.ssh_successes = 0
        self.last_probe = 0.0
        self.probe_interval = self._read_timeout_env(
            "SAOS_STATE_PROBE_INTERVAL_S", DEFAULT_PROBE_INTERVAL_S
        )
        self.passthrough_ssh_grace_s = self._read_timeout_env(
            "SAOS_PASSTHROUGH_SSH_GRACE_S", DEFAULT_PASSTHROUGH_SSH_GRACE_S
        )
        self.passthrough_ready_since = None
        self.startup_partial_config_path = os.environ.get("SAOS_STARTUP_CONFIG_PATH")
        self.startup_partial_retry_s = self._read_timeout_env(
            "SAOS_STARTUP_PARTIAL_RETRY_S", DEFAULT_STARTUP_PARTIAL_RETRY_S
        )
        self.startup_partial_last_attempt = 0.0
        self.startup_partial_applied = False
        self.startup_partial_config_loaded = False
        self.startup_partial_config = None
        self.startup_partial_error = None

        self.vms = [SAOS_vm(hostname, username, password, conn_mode, self.state_tracker)]

        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            new_soft = min(SOFT_ULIMIT_NOFILE, hard)
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            self.logger.info(f"ulimit -n set: soft={soft}, hard={hard}, new_soft={new_soft}")
        except ValueError as ve:
            self.logger.warning(f"Invalid value for ulimit -n: {ve}")
        except PermissionError as pe:
            self.logger.warning(f"Permission denied when setting ulimit -n: {pe}")
        except Exception as e:
            self.logger.warning(f"Unexpected error setting ulimit -n: {e}")

    def _read_timeout_env(self, name, default):
        value = os.environ.get(name)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError:
            self.logger.warning("Invalid %s=%s, using default %s", name, value, default)
            return default

    def _probe_port(self, host, port, timeout=1):
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except Exception:
            return False

    def _resolve_mgmt_ip(self):
        if not self.vms[0].mgmt_passthrough:
            return "127.0.0.1"
        ip_cidr = self.vms[0].mgmt_address_ipv4
        if not ip_cidr or ip_cidr == "dhcp":
            try:
                ip_cidr, _ = self.vms[0].get_mgmt_address()
            except Exception:
                return None
        if not ip_cidr or ip_cidr == "dhcp":
            return None
        return ip_cidr.split("/")[0]

    def _discover_startup_partial_config_path(self):
        config_dir = "/config"
        try:
            entries = os.listdir(config_dir)
        except Exception:
            return None
        candidates = [
            name for name in entries if ".partial" in name.lower()
        ]
        if not candidates:
            return None
        candidates.sort()
        if len(candidates) > 1:
            self.logger.warning(
                "Multiple partial configs found in %s; using %s",
                config_dir,
                candidates[0],
            )
        return os.path.join(config_dir, candidates[0])

    def _load_startup_partial_config(self):
        if self.startup_partial_config_loaded:
            return self.startup_partial_config
        self.startup_partial_config_loaded = True
        if not self.startup_partial_config_path:
            self.startup_partial_config_path = self._discover_startup_partial_config_path()
        if not self.startup_partial_config_path:
            return None
        if ".partial" not in os.path.basename(self.startup_partial_config_path).lower():
            self.startup_partial_error = (
                f"startup-config must be partial: {self.startup_partial_config_path}"
            )
            self.logger.error(self.startup_partial_error)
            return None
        if not os.path.exists(self.startup_partial_config_path):
            return None
        try:
            with open(self.startup_partial_config_path, "r") as fh:
                content = fh.read().strip()
        except Exception as exc:
            self.startup_partial_error = f"read failed: {exc}"
            self.logger.warning(
                "Failed to read startup config %s: %s",
                self.startup_partial_config_path,
                exc,
            )
            return None
        if not content:
            return None
        self.startup_partial_config = content
        return content

    def _prepare_netconf_config(self, config):
        stripped = config.strip()
        if re.match(r"^\s*<config[^>]*>.*</config>\s*$", stripped, re.DOTALL):
            return stripped
        return f"<config>{stripped}</config>"

    def _apply_startup_partial_config(self):
        if self.startup_partial_applied:
            return True
        if self.startup_partial_error:
            return False
        config = self._load_startup_partial_config()
        if not config:
            self.startup_partial_applied = True
            return True
        now = time.monotonic()
        if now - self.startup_partial_last_attempt < self.startup_partial_retry_s:
            return False
        self.startup_partial_last_attempt = now
        target_ip = self._resolve_mgmt_ip()
        if not target_ip:
            return False
        if not self._probe_port(target_ip, 830):
            return False
        netconf_import_error = None
        try:
            from scrapli_netconf.driver import NetconfDriver
        except Exception as exc:
            netconf_import_error = exc
            try:
                from scrapli.driver.netconf import NetconfDriver
            except Exception as exc2:
                self.startup_partial_error = (
                    f"netconf driver unavailable: {exc2}"
                )
                if netconf_import_error:
                    self.logger.error(
                        "NETCONF driver unavailable: %s; fallback error: %s",
                        netconf_import_error,
                        exc2,
                    )
                else:
                    self.logger.error("NETCONF driver unavailable: %s", exc2)
                return False
        driver = None
        try:
            driver_kwargs = {
                "host": target_ip,
                "port": 830,
                "auth_username": self.vms[0].username,
                "auth_password": self.vms[0].password,
                "auth_strict_key": False,
                "transport": "system",
                "timeout_socket": 60,
                "timeout_transport": 60,
                "timeout_ops": 60,
                "preferred_netconf_version": "1.0",
            }
            try:
                driver = NetconfDriver(**driver_kwargs)
            except TypeError:
                driver_kwargs.pop("transport", None)
                driver_kwargs.pop("preferred_netconf_version", None)
                try:
                    driver = NetconfDriver(**driver_kwargs)
                except TypeError:
                    driver_kwargs.pop("timeout_socket", None)
                    driver_kwargs.pop("timeout_transport", None)
                    driver_kwargs.pop("timeout_ops", None)
                    driver = NetconfDriver(**driver_kwargs)
            driver.open()
            response = driver.edit_config(
                config=self._prepare_netconf_config(config),
                target="running",
            )
            if getattr(response, "failed", False):
                result = getattr(response, "result", "")
                self.logger.error("NETCONF edit-config failed: %s", result)
                return False
        except Exception as exc:
            self.logger.warning("NETCONF apply failed: %s", exc)
            return False
        finally:
            if driver is not None:
                try:
                    driver.close()
                except Exception:
                    pass
        self.logger.info("Startup partial config applied via NETCONF")
        self.startup_partial_applied = True
        return True

    def _check_ssh_ready(self):
        if self.vms[0].mgmt_passthrough:
            if self.passthrough_ready_since is None:
                self.passthrough_ready_since = time.monotonic()
            elif time.monotonic() - self.passthrough_ready_since >= self.passthrough_ssh_grace_s:
                self.ssh_successes = 2
                self.state_tracker.set_state("ssh_ready")
            return
        now = time.monotonic()
        if now - self.last_probe < self.probe_interval:
            return
        self.last_probe = now
        target_ip = self._resolve_mgmt_ip()
        if not target_ip:
            return
        ssh_ok = self._probe_port(target_ip, 22)
        if ssh_ok:
            self.ssh_successes += 1
        else:
            self.ssh_successes = 0
        if self.ssh_successes >= 2:
            self.state_tracker.set_state("ssh_ready")

    def _update_health(self):
        if self.state_tracker.timed_out_state:
            self.update_health(
                1, f"unhealthy:timeout:{self.state_tracker.timed_out_state}"
            )
            return
        current = self.state_tracker.current
        if not current:
            self.update_health(1, "starting")
            return
        if self.health_mode == "progressive":
            if current == "healthy":
                self.update_health(0, "healthy")
            else:
                self.update_health(0, f"healthy:{current}")
            return
        if current == "healthy":
            self.update_health(0, "healthy")
        else:
            self.update_health(1, f"starting:{current}")

    def start(self):
        while True:
            for vm in self.vms:
                vm.work()

            if self.state_tracker.current is None:
                vm_start = self.vms[0].start_time
                if vm_start:
                    self.state_tracker.start_time = vm_start
                    self.state_tracker.set_state("waiting_for_login")

            self.state_tracker.check_timeout()

            if self.state_tracker.current == "config_base_applied":
                self._check_ssh_ready()
            if self.state_tracker.current == "ssh_ready":
                if self._apply_startup_partial_config():
                    self.state_tracker.set_state("startup_partial_applied")
            if self.state_tracker.current == "startup_partial_applied":
                self.state_tracker.set_state("healthy")

            self._update_health()

            if os.path.exists("/reset"):
                with open("/reset", "rt") as f:
                    fcontent = f.read().strip()
                vm_num_list = fcontent.split(",")
                for vm in self.vms:
                    if (str(vm.num) in vm_num_list) or not fcontent:
                        try:
                            if vm.use_scrapli:
                                vm.scrapli_qm.channel.write("system_reset\r")
                            else:
                                vm.qm.write("system_reset\r".encode())
                            self.logger.debug(
                                f"Sent qemu-monitor system_reset to VM num {vm.num} "
                            )
                        except Exception as e:
                            self.logger.error(
                                f"Failed to send qemu-monitor system_reset to VM num {vm.num} ({e})"
                            )
                try:
                    os.remove("/reset")
                except Exception as e:
                    self.logger.error(
                        f"Failed to cleanup /reset file({e}). qemu-monitor system_reset will likely be triggered again on VMs"
                    )


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

    vr = SAOS(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    cmd = [
            "uv",
            "run",
            "telnetproxy.py",
            "--remote-server",  "127.0.0.1",
            "--remote-port", "5100",
            "--listen-port", str(proxy_port)
    ]
    subprocess.Popen(cmd)
    start_telnet_loggers(ports=[proxy_port])
    vr.start()
