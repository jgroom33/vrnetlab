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
PASSWORD_BANNER_RE = re.compile(
    r"command execution and configuration are disabled until a new\s+password is configured",
    re.IGNORECASE | re.DOTALL,
)
EULA_BANNER_RE = re.compile(r"this special eula", re.IGNORECASE)
LIMITED_MODE_RE = re.compile(
    r"lost connection to netconf server|entering limited mode", re.IGNORECASE
)
PROMPT_PATTERNS = [
    re.compile(rb"BASE-dnx-SIM\??[>#]"),
    re.compile(rb"[A-Za-z0-9_.@:()~/\-]+\$"),
    re.compile(rb"[A-Za-z0-9_.@:()~/\-]+\??[>#]"),
]
PAGER_PATTERNS = [
    re.compile(rb"Enter:next line; Space:next page;", re.IGNORECASE),
    re.compile(rb"--More--"),
    re.compile(rb"Q:quit", re.IGNORECASE),
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
    "bootstrap_done": 300,
    "config_ready": 300,
    "config_base_applied": 900,
    "ssh_ready": 1200,
    "startup_partial_applied": 600,
    "healthy": 30,
}
DEFAULT_PROBE_INTERVAL_S = 5
DEFAULT_PASSTHROUGH_SSH_GRACE_S = 60
DEFAULT_STARTUP_PARTIAL_RETRY_S = 15
DEFAULT_NEW_PASSWORD = os.environ.get("SAOS_NEW_PASSWORD", "Ciena123!")
DEFAULT_PASSWORD_BOOTSTRAP_TIMEOUT = int(
    os.environ.get("SAOS_STATE_TIMEOUT_BOOTSTRAP_PASSWORD_S", "300")
)


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
        if self.timed_out_state:
            timed_out_idx = self._state_index(self.timed_out_state)
            new_idx = self._state_index(name)
            if (
                timed_out_idx is not None
                and new_idx is not None
                and new_idx >= timed_out_idx
            ):
                self.logger.info(
                    "STATE TIMEOUT CLEARED %s recovered at state=%s",
                    self.timed_out_state,
                    name,
                )
                self.timed_out_state = None
                self.timed_out_at = None
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

    def _state_index(self, name):
        try:
            return self.state_order.index(name)
        except ValueError:
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

    def reset_state_timer(self, name=None):
        target = name or self.current
        if not target:
            return False
        now = datetime.datetime.now()
        for entry in self.states:
            if entry["name"] == target:
                entry["ts"] = now
                self.timed_out_state = None
                self.timed_out_at = None
                self._write_state()
                self.logger.info("STATE %s timer reset ts=%s", target, now.isoformat())
                return True
        return False


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
        self.new_password = DEFAULT_NEW_PASSWORD
        self.password_changed = False

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

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

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
                self.wait_write(self.password, wait="Password:")
                output = self._wait_for_prompt(timeout=60, send_newline=True)
                if output is None:
                    self.logger.warning("login did not reach a prompt")
                    return
                if not self._login_reached_prompt(output):
                    self.logger.warning("login failed or prompt not reached")
                    return
                if self._password_change_required(output):
                    if not self._handle_password_change_banner():
                        return
                self.logger.debug("login complete")

                if not self.wait_for_bootstrap_done():
                    self.logger.warning(
                        "bootstrap did not complete; continuing with recovery flow"
                    )
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

    def _extend_state_timeout(self, key, minimum):
        if not self.state_tracker:
            return False
        current = self.state_tracker.timeouts.get(key)
        if current is None or current < minimum:
            self.state_tracker.timeouts[key] = minimum
            self.logger.info("STATE TIMEOUT %s extended to %ss", key, minimum)
            try:
                self.state_tracker._write_state()
            except Exception:
                pass
            return True
        return False

    def _reset_login_available_timer(self):
        if self.state_tracker:
            self.state_tracker.reset_state_timer("login_available")

    @staticmethod
    def _close_driver(driver):
        if driver is None:
            return
        try:
            driver.close()
        except Exception:
            pass

    def _init_driver_with_fallbacks(self, driver_cls, driver_kwargs, fallback_drop_sets):
        kwargs = dict(driver_kwargs)
        last_error = None
        for drop_keys in [()] + list(fallback_drop_sets):
            for key in drop_keys:
                kwargs.pop(key, None)
            try:
                return driver_cls(**kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return driver_cls(**kwargs)

    def _output_has_login_prompt(self, output):
        if not output:
            return False
        lower = output.lower()
        if EULA_BANNER_RE.search(lower):
            return True
        if LIMITED_MODE_RE.search(lower):
            return True
        if "login:" not in lower:
            return False
        for line in lower.splitlines():
            if "login:" in line and "last login:" not in line:
                return True
        return False

    def _recover_config_mode(self):
        self.logger.debug("session interrupted; attempting relogin and config-mode restore")
        if not self._relogin(timeout=60):
            return False
        op = self._enter_config_mode()
        return op is not None

    def _send_config_cmd(self, cmd, timeout=60):
        for _ in range(2):
            op = self._send_cmd_wait(cmd, timeout=timeout)
            if op is None:
                if not self._recover_config_mode():
                    return None
                continue
            if self._output_has_login_prompt(op) or self._prompt_is_sim(op):
                if not self._recover_config_mode():
                    return None
                continue
            return op
        return None

    def _login_reached_prompt(self, output):
        if not output:
            return False
        if self._output_has_login_prompt(output):
            return False
        if re.search(r"login incorrect|authentication failed", output, re.IGNORECASE):
            return False
        return (
            self._prompt_is_oper(output)
            or self._prompt_is_config(output)
            or self._prompt_is_sim(output)
        )

    def _password_change_required(self, output):
        if not output:
            return False
        return PASSWORD_BANNER_RE.search(output) is not None

    def _handle_password_change_banner(self):
        if self.password_changed:
            return True
        self.logger.warning("password change banner detected; updating password")
        self._reset_login_available_timer()
        self._extend_state_timeout("bootstrap_done", DEFAULT_PASSWORD_BOOTSTRAP_TIMEOUT)
        op = self._enter_config_mode()
        if op is None:
            self.logger.warning("unable to enter config mode for password change")
            return False
        if self.new_password == self.password:
            self.logger.warning("new password matches current; skipping password change")
            return False
        self._send_cmd_wait(
            f"system aaa authentication users user diag config password {self.new_password}",
            timeout=60,
        )
        if self._state_timed_out():
            return False
        self._reset_login_available_timer()
        self.password = self.new_password
        self.password_changed = True
        if not self._exit_to_oper(timeout=10, max_exits=4):
            self.logger.warning("unable to exit config mode after password change")
        # Some releases do not reliably present a clean login prompt
        # immediately after password update. Keep the current session when
        # possible, and only force a re-login if we cannot confirm a prompt.
        post_change_output = self._wait_for_prompt(timeout=15, send_newline=True)
        if not self._login_reached_prompt(post_change_output):
            if not self._relogin(password=self.password, timeout=60):
                self.logger.warning("login failed after password change")
                return False
        self._wait_for_non_sim_prompt(timeout=30)
        return True

    def wait_for_bootstrap_done(self):
        while True:
            if self._state_timed_out():
                return False
            op = self._send_cmd_wait("show bootstrap-status", timeout=60)
            if self._password_change_required(op):
                if not self._handle_password_change_banner():
                    return False
                continue
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
            idx, match, _ = self.tn.expect(
                [b"Password:", b"login:"], timeout=min(5, end - time.time())
            )
            if match and idx == 0:
                return True
            if match and idx == 1:
                # EULA/login loops may re-prompt for username before password.
                user = getattr(self, "username", "diag")
                try:
                    self.tn.write(f"{user}\r".encode())
                except Exception:
                    return False
                continue
            try:
                self.tn.write(b"\r")
            except Exception:
                pass
        return False

    def _relogin(self, password=None, timeout=60):
        if password is None:
            password = self.password
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
        output = self._wait_for_prompt(timeout=60, send_newline=True)
        if not self._login_reached_prompt(output):
            return False
        self.logger.debug("login complete")
        return True

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
            if self._output_has_login_prompt(op):
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
        ipv4 = None
        subnet = None
        if self.mgmt_passthrough and not self.mgmt_dhcp:
            ipv4, subnet = self.mgmt_address_ipv4.split("/")

        attempts = 3
        for attempt in range(1, attempts + 1):
            self.logger.info(
                "applying base configuration (attempt %s/%s)", attempt, attempts
            )
            self._wait_for_prompt(timeout=60, send_newline=True)
            op = self._enter_config_mode()
            if self._state_timed_out():
                return False
            if op is None:
                self.logger.debug("config not ready, attempting re-login")
                if not self._relogin():
                    self.logger.warning("unable to login for base config")
                    if attempt >= attempts:
                        return False
                    continue
                op = self._enter_config_mode()
                if self._state_timed_out():
                    return False
            if op is None:
                self.logger.warning("unable to enter config mode")
                if attempt >= attempts:
                    return False
                continue

            if op and (not self.password_changed) and self._password_change_required(op):
                if not self._handle_password_change_banner():
                    return False
                if attempt >= attempts:
                    return False
                continue

            if self.state_tracker:
                self.state_tracker.set_state("config_ready")

            base_ok = True
            hostname_out = self._send_config_cmd(
                f"system config hostname {self.hostname}", timeout=60
            )
            if hostname_out is None or self._output_has_cli_error(hostname_out):
                base_ok = False
            if self._state_timed_out():
                return False
            if ipv4 and subnet:
                dhcp_out = self._send_config_cmd(
                    "dhcp-client client mgmtbr0 admin-enable false", timeout=60
                )
                if dhcp_out is None or self._output_has_cli_error(dhcp_out):
                    base_ok = False
                if self._state_timed_out():
                    return False
                addr_out = self._send_config_cmd(
                    f"oc-if:interfaces interface mgmtbr0 ipv4 addresses address {ipv4} "
                    f"config ip {ipv4} prefix-length {subnet}",
                    timeout=60,
                )
                if addr_out is None or self._output_has_cli_error(addr_out):
                    base_ok = False
                if self._state_timed_out():
                    return False
                route_out = self._send_config_cmd(
                    f"rib vrf default ipv4 0.0.0.0/0 next-hop {re.sub(r'\d+$', '1', ipv4)}",
                    timeout=60,
                )
                if route_out is None or self._output_has_cli_error(route_out):
                    base_ok = False
                if self._state_timed_out():
                    return False
            if not self._exit_to_oper(timeout=30, max_exits=12):
                base_ok = False
            if base_ok and ipv4 and subnet:
                if not self._verify_mgmt_base_config(
                    ipv4=ipv4,
                    gateway=re.sub(r"\d+$", "1", ipv4),
                ):
                    base_ok = False
            if base_ok:
                if self.state_tracker:
                    self.state_tracker.set_state("config_base_applied")
                return True

            self.logger.warning(
                "base configuration attempt %s/%s did not complete", attempt, attempts
            )
            if attempt < attempts:
                self._relogin(timeout=30)
                continue
            return False
        return False

    def _verify_mgmt_base_config(self, ipv4, gateway):
        iface_out = self._send_cmd_wait("show ip interfaces interface mgmtbr0", timeout=60)
        if iface_out is None or self._output_has_cli_error(iface_out):
            self.logger.warning("mgmt base verification failed: interface output unavailable")
            return False
        route_out = self._send_cmd_wait("show ip route", timeout=60)
        if route_out is None or self._output_has_cli_error(route_out):
            self.logger.warning("mgmt base verification failed: route output unavailable")
            return False
        if ipv4 not in iface_out:
            self.logger.warning(
                "mgmt base verification failed: expected mgmt IP %s not present", ipv4
            )
            return False
        if (
            "0.0.0.0/0" not in route_out
            or gateway not in route_out
            or "mgmtbr0" not in route_out
        ):
            self.logger.warning(
                "mgmt base verification failed: expected default route via %s missing",
                gateway,
            )
            return False
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

    def _output_has_cli_error(self, output):
        if not output:
            return True
        lower = output.lower()
        return (
            "shell parser failure" in lower
            or "no matching entry found" in lower
            or "% error" in lower
            or "config mode error" in lower
        )

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
            if op and self._output_has_login_prompt(op):
                if not self._relogin(timeout=30):
                    return False
                op = self._wait_for_prompt(timeout=10, send_newline=True)
                if op and self._prompt_is_oper(op):
                    return True
                continue
            if op and self._prompt_is_oper(op):
                return True
        op = self._wait_for_prompt(timeout=timeout, send_newline=True)
        if op and self._prompt_is_oper(op):
            return True
        return False

    def _send_cmd_wait(self, cmd, timeout=30, settle_s=0):
        self.logger.debug("writing to serial console: '%s'", cmd)
        pre_output = b""
        try:
            pre_output = self.tn.read_very_eager() or b""
        except Exception:
            pass
        self.tn.write(f"{cmd}\r".encode())
        end = time.time() + timeout
        buffer = b""
        matched_prompt = False
        patterns = PROMPT_PATTERNS + PAGER_PATTERNS
        while time.time() < end:
            remaining = max(1, int(end - time.time()))
            idx, match, data = self.tn.expect(patterns, timeout=min(5, remaining))
            if data:
                buffer += data
            if not match:
                continue
            if idx >= len(PROMPT_PATTERNS):
                # Consume paged output so long "show" commands include full text.
                try:
                    self.tn.write(b" ")
                except Exception:
                    pass
                continue
            matched_prompt = True
            if settle_s > 0:
                settle_end = min(end, time.time() + settle_s)
                while time.time() < settle_end:
                    wait = max(0.1, min(0.5, settle_end - time.time()))
                    idx2, match2, data2 = self.tn.expect(patterns, timeout=wait)
                    if data2:
                        buffer += data2
                        settle_end = min(end, time.time() + settle_s)
                    if not match2:
                        continue
                    if idx2 >= len(PROMPT_PATTERNS):
                        try:
                            self.tn.write(b" ")
                        except Exception:
                            pass
                        settle_end = min(end, time.time() + settle_s)
                    else:
                        settle_end = min(end, time.time() + settle_s)
            break
        output = buffer.decode(errors="ignore") if matched_prompt else None
        if output is None:
            output = self._wait_for_prompt(timeout=10, send_newline=True)
        if pre_output:
            prefix = pre_output.decode(errors="ignore")
            output = f"{prefix}{output or ''}"
        return output


class SAOS(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super().__init__(username, password)

        state_timeouts = {
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
            timeouts=state_timeouts,
            state_file="/state.json",
        )
        self.ssh_successes = 0
        self.ssh_probe_failures = 0
        self.next_ssh_probe_at = 0.0
        self.probe_interval = self._read_timeout_env(
            "SAOS_STATE_PROBE_INTERVAL_S", DEFAULT_PROBE_INTERVAL_S
        )
        self.passthrough_ssh_grace_s = self._read_timeout_env(
            "SAOS_PASSTHROUGH_SSH_GRACE_S", DEFAULT_PASSTHROUGH_SSH_GRACE_S
        )
        self.startup_partial_config_path = os.environ.get("SAOS_STARTUP_CONFIG_PATH")
        self.startup_partial_retry_s = self._read_timeout_env(
            "SAOS_STARTUP_PARTIAL_RETRY_S", DEFAULT_STARTUP_PARTIAL_RETRY_S
        )
        self.startup_partial_last_attempt = 0.0
        self.startup_partial_applied = False
        self.startup_partial_config_loaded = False
        self.startup_partial_config = None
        self.startup_partial_config_kind = None
        self.startup_partial_error = None
        self.startup_partial_external_required = False
        self.startup_partial_external_target = None
        self.startup_partial_external_reason = None
        self.ssh_probe_blind_spot_marked = False
        self.startup_partial_passthrough_timeout_s = self._read_timeout_env(
            "SAOS_STATE_TIMEOUT_STARTUP_PARTIAL_PASSTHROUGH_S", 600
        )

        self.vms = [SAOS_vm(hostname, username, password, conn_mode, self.state_tracker)]
        if self.vms[0].mgmt_passthrough:
            timeout_s = self.state_tracker.timeouts.get("startup_partial_applied", 0)
            if timeout_s < self.startup_partial_passthrough_timeout_s:
                self.state_tracker.timeouts["startup_partial_applied"] = (
                    self.startup_partial_passthrough_timeout_s
                )
                self.logger.info(
                    "STATE TIMEOUT startup_partial_applied set to %ss for passthrough mode",
                    self.startup_partial_passthrough_timeout_s,
                )
                try:
                    self.state_tracker._write_state()
                except Exception:
                    pass

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

    @staticmethod
    def _close_driver(driver):
        if driver is None:
            return
        try:
            driver.close()
        except Exception:
            pass

    def _init_driver_with_fallbacks(self, driver_cls, driver_kwargs, fallback_drop_sets):
        kwargs = dict(driver_kwargs)
        last_error = None
        for drop_keys in [()] + list(fallback_drop_sets):
            for key in drop_keys:
                kwargs.pop(key, None)
            try:
                return driver_cls(**kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return driver_cls(**kwargs)

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

    def _local_mgmt_ip(self):
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
        self.startup_partial_config_kind = self._detect_startup_partial_config_kind(content)
        return content

    def _detect_startup_partial_config_kind(self, config):
        if re.match(r"^\s*<", config):
            return "netconf"
        return "cli"

    def _prepare_netconf_config(self, config):
        stripped = config.strip()
        if re.match(r"^\s*<config[^>]*>.*</config>\s*$", stripped, re.DOTALL):
            return stripped
        return f"<config>{stripped}</config>"

    def _prepare_cli_config(self, config):
        lines = []
        for raw in config.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("!"):
                continue
            if line.lower() in ("config", "configure"):
                continue
            lines.append(line)
        return lines

    def _cli_prompt_pattern(self):
        return r"^\S+\??[>#]\s*$|^\S+\$\s*$"

    def _cli_output_failed(self, output):
        if not output:
            return True
        lower = output.lower()
        return (
            "shell parser failure" in lower
            or "no matching entry found" in lower
            or "% error" in lower
        )

    def _startup_apply_blind_spot(self, target_ip):
        return (
            self.vms[0].mgmt_passthrough
            and target_ip
            and target_ip == self._local_mgmt_ip()
        )

    def _update_state_meta(self, **kwargs):
        if not self.state_tracker:
            return
        self.state_tracker.meta.update(kwargs)
        try:
            self.state_tracker._write_state()
        except Exception:
            pass

    def _mark_startup_partial_external_required(self, target_ip):
        reason = (
            "startup partial apply requires external controller: "
            f"in-container access to {target_ip} is not possible in mgmt passthrough mode; "
            "apply config externally via SSH/NETCONF to the VM IP"
        )
        if self.startup_partial_external_required:
            return
        self.startup_partial_external_required = True
        self.startup_partial_external_target = target_ip
        self.startup_partial_external_reason = reason
        self.logger.warning(reason)
        self._update_state_meta(
            startup_partial_external_required=True,
            startup_partial_external_target=target_ip,
            startup_partial_external_reason=reason,
        )

    def _mark_ssh_probe_blind_spot(self, target_ip):
        if self.ssh_probe_blind_spot_marked:
            return
        self.ssh_probe_blind_spot_marked = True
        reason = (
            "in-container SSH probe blind-spot in mgmt passthrough mode: "
            f"target {target_ip} resolves as local in the container namespace; "
            "validate reachability from host-side probes"
        )
        self._update_state_meta(
            ssh_probe_blind_spot=True,
            ssh_probe_blind_spot_target=target_ip,
            ssh_probe_blind_spot_reason=reason,
        )

    def _apply_startup_partial_config_cli(self, target_ip, config):
        if not self._probe_port(target_ip, 22):
            self.logger.debug("CLI startup apply waiting for SSH on %s:22", target_ip)
            return False
        try:
            from scrapli.driver.generic import GenericDriver
        except Exception as exc:
            self.startup_partial_error = f"cli driver unavailable: {exc}"
            self.logger.error("CLI driver unavailable: %s", exc)
            return False
        driver = None
        try:
            driver_kwargs = {
                "host": target_ip,
                "port": 22,
                "auth_username": self.vms[0].username,
                "auth_password": self.vms[0].password,
                "auth_strict_key": False,
                "transport": "system",
                "timeout_socket": 60,
                "timeout_transport": 60,
                "timeout_ops": 60,
                "comms_prompt_pattern": self._cli_prompt_pattern(),
            }
            driver = self._init_driver_with_fallbacks(
                GenericDriver,
                driver_kwargs,
                [
                    ("transport", "comms_prompt_pattern"),
                    ("timeout_socket", "timeout_transport", "timeout_ops"),
                ],
            )
            driver.open()
            response = driver.send_command("config", timeout_ops=60)
            if self._cli_output_failed(response.result):
                self.logger.warning("CLI apply failed: unable to enter config mode")
                return False
            lines = self._prepare_cli_config(config)
            if not lines:
                self.logger.info("CLI startup partial config empty, skipping")
                return True
            for line in lines:
                response = driver.send_command(line, timeout_ops=60)
                if self._cli_output_failed(response.result):
                    self.logger.warning("CLI apply failed on command: %s", line)
                    return False
            for _ in range(3):
                driver.send_command("exit", timeout_ops=30)
        except Exception as exc:
            self.logger.warning("CLI apply raised %s", exc)
            return False
        finally:
            self._close_driver(driver)
        self.logger.info("Startup partial config applied via CLI")
        return True

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
            self.logger.debug("startup partial apply waiting for management IP")
            return False
        if self._startup_apply_blind_spot(target_ip):
            self._mark_startup_partial_external_required(target_ip)
            # In passthrough mode this container cannot reach the VM IP directly.
            # Mark this stage complete so external orchestrators can apply config.
            self.startup_partial_applied = True
            return True
        if self.startup_partial_config_kind == "cli":
            if self._apply_startup_partial_config_cli(target_ip, config):
                self.startup_partial_applied = True
                return True
            return False
        if not self._probe_port(target_ip, 830):
            self.logger.debug("NETCONF startup apply waiting for NETCONF on %s:830", target_ip)
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
            driver = self._init_driver_with_fallbacks(
                NetconfDriver,
                driver_kwargs,
                [
                    ("transport", "preferred_netconf_version"),
                    ("timeout_socket", "timeout_transport", "timeout_ops"),
                ],
            )
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
            self._close_driver(driver)
        self.logger.info("Startup partial config applied via NETCONF")
        self.startup_partial_applied = True
        return True

    def _check_ssh_ready(self):
        now = time.monotonic()
        if now < self.next_ssh_probe_at:
            return
        target_ip = self._resolve_mgmt_ip()
        if not target_ip:
            backoff_s = (
                self.passthrough_ssh_grace_s
                if self.vms[0].mgmt_passthrough
                else self.probe_interval
            )
            self.next_ssh_probe_at = now + backoff_s
            return
        blind_spot = (
            self.vms[0].mgmt_passthrough and target_ip == self._local_mgmt_ip()
        )
        ssh_ok = self._probe_port(target_ip, 22)
        if ssh_ok:
            self.ssh_successes += 1
            self.ssh_probe_failures = 0
            self.next_ssh_probe_at = now + self.probe_interval
        else:
            self.ssh_successes = 0
            self.ssh_probe_failures += 1
            backoff_s = (
                self.passthrough_ssh_grace_s
                if self.vms[0].mgmt_passthrough
                else self.probe_interval
            )
            self.next_ssh_probe_at = now + backoff_s
            if blind_spot and self.ssh_probe_failures == 1:
                self.logger.warning(
                    "ssh probe blind-spot in passthrough mode for %s; host-side reachability should be validated externally",
                    target_ip,
                )
                self._mark_ssh_probe_blind_spot(target_ip)
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
                target_ip = self._resolve_mgmt_ip()
                # In mgmt passthrough, in-container probing can be blind for the VM IP.
                # Skip internal ssh gating and transition via external-required startup apply.
                if self._startup_apply_blind_spot(target_ip):
                    self._mark_ssh_probe_blind_spot(target_ip)
                    if self._apply_startup_partial_config():
                        self.state_tracker.set_state("startup_partial_applied")
                else:
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
