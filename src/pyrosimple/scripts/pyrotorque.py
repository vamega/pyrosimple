# pylint: disable=attribute-defined-outside-init
""" rTorrent queue manager & daemon.

    Copyright (c) 2012 The PyroScope Project <pyroscope.project@gmail.com>
"""

import logging
import os
import signal
import sys
import time

from pathlib import Path
from typing import Dict

from apscheduler.schedulers.background import BackgroundScheduler
from box.box import Box
from daemon import DaemonContext
from daemon.pidfile import TimeoutPIDLockFile

from pyrosimple import config, error
from pyrosimple.scripts.base import ScriptBaseWithConfig
from pyrosimple.util import logutil, pymagic


class RtorrentQueueManager(ScriptBaseWithConfig):
    """
    rTorrent queue manager & daemon.
    """

    POLL_TIMEOUT = 1.0

    RUNTIME_DIR = os.getenv("XDG_RUNTIME_DIR") or "~/.pyrosimple/run/"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.classes = {}
        self.jobs: Dict = {}

    def add_options(self):
        """Add program options."""
        super().add_options()

        # basic options
        self.add_bool_option(
            "-n",
            "--dry-run",
            help="advise jobs not to do any real work, just tell what would happen",
        )
        self.add_bool_option(
            "--no-fork",
            "--fg",
            help="Don't fork into background (stay in foreground and log to console)",
        )
        self.add_value_option(
            "--run-once", "JOB", help="run the specified job once in the foreground"
        )
        self.add_bool_option("--stop", help="stop running daemon")
        self.add_bool_option(
            "--restart",
            help="stop any existing daemon, then start the process in the backgrounds",
        )
        self.add_bool_option("-?", "--status", help="Check daemon status")
        self.add_value_option(
            "--pid-file",
            "PATH",
            help="file holding the process ID of the daemon, when running in background",
        )

    def parse_schedule(self, schedule):
        """Parse a job schedule."""
        result = {}

        for param in schedule.split():
            param = param.strip()
            try:
                key, val = param.split("=", 1)
                if key == "jitter":
                    val = int(val)
            except (TypeError, ValueError) as exc:
                raise error.ConfigurationError(
                    f"Bad param '{param}' in job schedule '{schedule}'"
                ) from exc
            result[key] = val

        return result

    def validate_config(self):
        """Handle and check configuration."""

        for name, params in config.settings.TORQUE.items():
            # Skip non-dictionary keys
            if not isinstance(params, Box):
                continue
            for key in ("handler", "schedule"):
                if key not in params:
                    raise error.ConfigurationError(
                        f"Job '{name}' is missing the required '{key}' parameter"
                    )
            self.jobs[name] = dict(params)
            if self.options.dry_run:
                self.jobs[name]["dry_run"] = True
            if params.get("active", True):
                self.jobs[name]["__handler"] = pymagic.import_name(params.handler)
            self.jobs[name]["schedule"] = self.parse_schedule(params.get("schedule"))

    def add_jobs(self):
        """Add configured jobs."""
        for name, params in self.jobs.items():
            if params.get("active", True):
                params.setdefault("__job_name", name)
                # Keep track of the instantiated class for cleanup later
                self.classes[name] = params["__handler"](params)
                self.sched.add_job(
                    self.classes[name].run,
                    name=name,
                    id=name,
                    trigger="cron",
                    **params["schedule"],
                )
                print(self.jobs[name])

    def unload_jobs(self):
        """Allows jobs classes to clean up any global resources if the
        cleanup() method exists.

        This should be called only once the jobs have finished
        running, so that a successive run doesn't re-create the
        resources.

        """
        for _, cls in self.classes.items():
            if hasattr(cls, "cleanup") and callable(cls.cleanup):
                cls.cleanup()

    def reload_jobs(self):
        """Reload the configured jobs gracefully."""
        try:
            config.settings.configure()
            if self.running_config != dict(config.settings.TORQUE):
                self.log.info("Config change detected, reloading jobs")
                self.validate_config()
                self.sched.pause()
                self.sched.remove_all_jobs()
                self.unload_jobs()
                self.sched.resume()
                self.add_jobs()
                self.running_config = dict(config.settings.TORQUE)
        except (Exception) as exc:  # pylint: disable=broad-except
            self.log.error("Error while reloading config: %s", exc)
        else:
            self.sched.resume()

    def run_forever(self):
        """Run configured jobs until termination request."""
        self.running_config = dict(config.settings.TORQUE)
        while True:
            try:
                time.sleep(self.POLL_TIMEOUT)
                if config.settings.TORQUE.get("autoreload", False):
                    self.reload_jobs()
            except KeyboardInterrupt as exc:
                self.log.info("Termination request received (%s)", exc)
                self.sched.shutdown()
                self.unload_jobs()
                break
            except SystemExit as exc:
                self.return_code = exc.code or 0
                self.log.info("System exit (RC=%r)", self.return_code)
                break

    def mainloop(self):
        """The main loop."""
        try:
            self.validate_config()
        except (error.ConfigurationError) as exc:
            self.fatal(exc)

        # Defaults for process control paths
        if not self.options.pid_file:
            self.options.pid_file = TimeoutPIDLockFile(
                Path(self.RUNTIME_DIR, "pyrotorque.pid").expanduser()
            )

        # Process control
        if self.options.status or self.options.stop or self.options.restart:
            if self.options.pid_file.is_locked():
                running, pid = True, self.options.pid_file.read_pid()
            else:
                running, pid = False, 0

            if self.options.status:
                if running:
                    self.log.info("Pyrotorque is running (PID %d).", pid)
                    sys.exit(0)
                else:
                    self.log.error("No pyrotorque process found.")
                    sys.exit(1)

            if self.options.stop or self.options.restart:
                if running:
                    os.kill(pid, signal.SIGTERM)
                    self.log.debug("Process %d sent SIGTERM.", pid)

                    # Wait for termination (max. 10 secs)
                    for _ in range(100):
                        if not self.options.pid_file.is_locked():
                            running = False
                            break
                        time.sleep(0.1)

                    self.log.info("Process %d stopped.", pid)
                elif pid:
                    self.log.info("Process %d NOT running anymore.", pid)
                else:
                    self.log.info(
                        "No pid file '%s'", (self.options.pid_file or "<N/A>")
                    )
            else:
                self.log.info(
                    "Process %d %s running.", pid, "UP and" if running else "NOT"
                )

            if self.options.stop:
                self.return_code = error.EX_OK if running else error.EX_UNAVAILABLE
                return

        # Check if we only need to run once
        if self.options.run_once:
            params = self.jobs[self.options.run_once]
            if self.options.dry_run:
                params["dry_run"] = True
            params["__handler_copy"] = params.get("__handler")(params)
            params["__handler_copy"].run()
            sys.exit(0)

        dcontext = DaemonContext(
            detach_process=False,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        # Detach, if not disabled via option
        if not self.options.no_fork:
            dcontext.detach_process = True
            dcontext.stdin = None
            dcontext.stderr = logutil.get_logfile()
            dcontext.stdout = logutil.get_logfile()
            dcontext.pidfile = self.options.pid_file
            self.log.info(
                "Writing pid to %s and detaching process...", self.options.pid_file
            )
            self.log.info("Logging stderr/stdout to %s", logutil.get_logfile())

        # Change logging format
        logging.basicConfig(
            force=True, format="%(asctime)s %(levelname)5s %(name)s: %(message)s"
        )

        with dcontext:
            # Set up services
            self.sched = BackgroundScheduler()

            # Run services
            self.sched.start()
            try:
                self.add_jobs()
                self.run_forever()
            finally:
                self.sched.shutdown()


def run():  # pragma: no cover
    """The entry point."""
    RtorrentQueueManager().run()


if __name__ == "__main__":
    run()
