#!/usr/bin/env python3

import glob
import json
import logging
import os
import threading
from typing import Any, List, Optional

log = logging.getLogger("DAMXDaemon")
STATE_FILE = "/var/lib/damx/power_state.json"

_NON_AC_TYPES = {"Battery", "UPS"}


class PowerSourceDetector:
    DEFAULT_AC_FALLBACKS: List[str] = ["balanced-performance", "balanced", "quiet"]
    DEFAULT_BAT_FALLBACKS: List[str] = ["low-power", "balanced"]

    def __init__(self, manager: Any, poll_interval: float = 2.0) -> None:
        self.manager: Any = manager
        self.poll_interval: float = poll_interval

        self.is_ac: Optional[bool] = None
        self.last_ac_profile: str = "balanced-performance"
        self.last_battery_profile: str = "low-power"

        self._lock: threading.RLock = threading.RLock()
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._load_state()
        log.info(
            "PowerSourceDetector initialized (AC target: '%s', Battery target: '%s')",
            self.last_ac_profile,
            self.last_battery_profile,
        )

    def _load_state(self) -> None:
        if not os.path.exists(STATE_FILE):
            return

        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.last_ac_profile = data.get("last_ac_profile", self.last_ac_profile)
                self.last_battery_profile = data.get("last_battery_profile", self.last_battery_profile)
        except Exception as e:
            log.warning("Could not load power state file %s: %s", STATE_FILE, e)

    def _save_state(self) -> None:
        state_dir = os.path.dirname(STATE_FILE)
        tmp_file = f"{STATE_FILE}.tmp.{os.getpid()}"
        data = {
            "last_ac_profile": self.last_ac_profile,
            "last_battery_profile": self.last_battery_profile,
        }

        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, STATE_FILE)

            try:
                dir_fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

        except Exception as e:
            log.error("Failed to save power state file: %s", e)
            if os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    def is_plugged_in(self) -> bool:
        for type_path in glob.glob("/sys/class/power_supply/*/type"):
            try:
                with open(type_path, "r", encoding="utf-8") as tf:
                    dev_type = tf.read().strip()

                if dev_type in _NON_AC_TYPES:
                    continue

                online_path = type_path.replace("/type", "/online")
                if os.path.exists(online_path):
                    with open(online_path, "r", encoding="utf-8") as of:
                        if of.read().strip() == "1":
                            return True
            except OSError:
                continue
        return False

    def start_monitoring(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.debug("start_monitoring called but a poller thread is already running.")
                return

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="DAMX-PowerPoller",
            )
            self._thread.start()
            log.info("Monitoring power source started.")

    def stop_monitoring(self) -> None:
        thread = self._thread
        if thread is None:
            log.debug("stop_monitoring called but no poller thread was running.")
            return

        self._stop_event.set()
        thread.join(timeout=self.poll_interval + 1.0)

        if thread.is_alive():
            log.warning(
                "Poller thread did not exit within %.1fs; refusing to clear reference.",
                self.poll_interval + 1.0,
            )
            return

        self._thread = None
        log.info("Monitoring power source stopped.")

    def _monitor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                current_ac = self.is_plugged_in()

                with self._lock:
                    if current_ac != self.is_ac:
                        self.is_ac = current_ac
                        self._apply_power_profile(self.is_ac)

            except Exception as e:
                log.error("Error in power monitor loop: %s", e)

            self._stop_event.wait(self.poll_interval)

    def _apply_power_profile(self, is_ac: bool) -> None:
        if (
            not hasattr(self.manager, "available_features")
            or "thermal_profile" not in self.manager.available_features
        ):
            return

        try:
            choices = self.manager.get_thermal_profile_choices() or []
        except Exception as e:
            log.error("Failed to query available thermal profiles: %s", e)
            return

        preferred = self.last_ac_profile if is_ac else self.last_battery_profile
        target: Optional[str] = None

        if preferred in choices:
            target = preferred
        else:
            fallbacks = self.DEFAULT_AC_FALLBACKS if is_ac else self.DEFAULT_BAT_FALLBACKS
            for fallback in fallbacks:
                if fallback in choices:
                    target = fallback
                    break

        if target:
            state_label = "AC" if is_ac else "Battery"
            log.info("Power transition (%s) -> setting thermal profile '%s'", state_label, target)
            try:
                self.manager.set_thermal_profile(target)
                if is_ac:
                    self.last_ac_profile = target
                else:
                    self.last_battery_profile = target
                self._save_state()
            except Exception as e:
                log.error("Failed applying profile '%s': %s", target, e)
        else:
            log.warning(
                "No compatible profile found for %s (Available: %s)",
                "AC" if is_ac else "Battery",
                choices,
            )

    def on_user_profile_change(self, profile: str) -> None:
        with self._lock:
            if self.is_ac is None:
                self.is_ac = self.is_plugged_in()

            if self.is_ac:
                self.last_ac_profile = profile
                log.info("Saved explicit user AC preference: '%s'", profile)
            else:
                self.last_battery_profile = profile
                log.info("Saved explicit user Battery preference: '%s'", profile)

            self._save_state()
