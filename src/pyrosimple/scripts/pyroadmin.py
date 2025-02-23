""" Administration Tool.

    Copyright (c) 2010 The PyroScope Project <pyroscope.project@gmail.com>
"""

import importlib.resources
import re

from datetime import datetime
from pathlib import Path
from xmlrpc import client as xmlrpclib

import tomli_w

import pyrosimple

from pyrosimple import config
from pyrosimple.scripts.base import ScriptBase
from pyrosimple.util import matching


class AdminTool(ScriptBase):
    """Support for administrative tasks."""

    # TODO: config create, dump, set, get
    # TODO: backup session/config

    def add_options(self):
        super().add_options()
        self.parser.add_argument("-U", "--url", help="URL to rtorrent instance")
        self.parser.set_defaults(func=None)
        subparsers = self.parser.add_subparsers()
        config_parser = subparsers.add_parser("config", help="Validate configuration")
        config_parser.set_defaults(func=self.config)
        config_parser.add_argument(
            "--check", help="Check config for any issues", action="store_true"
        )
        config_parser.add_argument(
            "--dump-rc", help="Print out the full rTorrent config", action="store_true"
        )
        config_parser.add_argument(
            "--create-config",
            help="Create config.toml in the default location if it does not exist",
            action="store_true",
        )
        config_parser.add_argument(
            "--create-rtorrent-rc",
            help="Create a rtorrent.rc in the default location if it does not exist",
            action="store_true",
        )
        backfill_parser = subparsers.add_parser(
            "backfill", help="Backfill missing custom fields from available data"
        )
        backfill_parser.set_defaults(func=self.backfill)
        backfill_parser.add_argument(
            "--dry-run",
            help="Print changes instead of applying them",
            action="store_true",
        )

    def dump_rc(self):
        """Print a representative .rtorrent.rc as gleaned from a running instance.

        This is neat but somewhat brittle, and behaves differently between XMLRPC and JSON-RPC."""

        proxy = pyrosimple.connect().open()
        methods = proxy.system.listMethods()
        # XXX This is a heuristic and might break in newer rTorrent versions!
        builtins = set(methods[: methods.index("view.sort_new") + 1])
        methods = set(methods)
        plain_re = re.compile(r"^[a-zA-Z0-9_.]+$")
        RC_CONTINUATION_THRESHOLD = 50

        def is_method(name):
            "Helper"
            prefixes = (
                "d.",
                "f.",
                "p.",
                "t.",
                "choke_group.",
                "session.",
                "system.",
                "throttle.",
                "trackers.",
                "ui.",
                "view.",
            )

            if name.endswith("="):
                name = name[:-1]
            return plain_re.match(name) and (
                name in methods or any(name.startswith(x) for x in prefixes)
            )

        def rc_quoted(text, in_brace=False):
            "Helper"
            if isinstance(text, list):
                wrap_fmt = "{%s}"
                try:
                    method_name = text[0] + ""
                except (TypeError, IndexError):
                    pass
                else:
                    if is_method(method_name):
                        wrap_fmt = "(%s)" if in_brace else "((%s))"
                        if (
                            ".set" not in method_name
                            and len(text) == 2
                            and text[1] == 0
                        ):
                            text = text[:1]
                text = wrap_fmt % ", ".join(
                    [rc_quoted(x, in_brace=(wrap_fmt[0] == "{")) for x in text]
                )
                return text.replace("))))", ")) ))")
            elif isinstance(text, int):
                return "(value, {:d})".format(text)
            elif plain_re.match(text) or is_method(text):
                return text
            else:
                return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))

        group = None
        for name in sorted(methods):
            try:
                value = proxy.method.get("", name)
                const = bool(proxy.method.const("", name))
            except xmlrpclib.Fault as exc:
                if "Key not found" in exc.faultString:
                    continue
                raise
            group, old_group = name.split(".", 1)[0], group
            if group == "event":
                group = name
            if group != old_group:
                print("")

            definition = None
            objtype = type(value)
            if objtype is list:
                value = [rc_quoted(x) for x in value]
                wrap_fmt = "((%s))" if value and is_method(value[0]) else "{%s}"
                definition = wrap_fmt % ", ".join(value)
            elif objtype is dict:
                print("method.insert = {}, multi|rlookup|static".format(name))
                for key, val in sorted(value.items()):
                    val = rc_quoted(val)
                    if len(val) > RC_CONTINUATION_THRESHOLD:
                        val = "\\\n    " + val
                    print('method.set_key = {}, "{}", {}'.format(name, key, val))
            elif objtype is str:
                definition = rc_quoted(value)
            elif objtype is int:
                definition = "{:d}".format(value)
            else:
                self.log.error(
                    "Cannot handle {!r} definition of method {}".format(objtype, name)
                )
                continue

            if definition:
                if name in builtins:
                    print("{}.set = {}".format(name, definition))
                else:
                    rctype = {str: "string", int: "value"}.get(objtype, "simple")
                    if const:
                        rctype += "|const"
                        const = None
                    if len(definition) > RC_CONTINUATION_THRESHOLD:
                        definition = "\\\n    " + definition
                    definition = definition.replace(" ;     ", " ;\\\n     ").replace(
                        ",    ", ",\\\n    "
                    )
                    print("method.insert = {}, {}, {}".format(name, rctype, definition))
            if const:
                print("method.const.enable = {}".format(name))

    def backfill(self):
        """Backfill missing any missing metadata from available sources.
        Safe to run multiple times.
        """
        # pylint: disable=broad-except
        if self.options.url:
            config.settings["SCGI_URL"] = config.lookup_connection_alias(
                self.options.url
            )
        engine = pyrosimple.connect()
        engine.open()
        for i in engine.view("main", matching.create_matcher("loaded=0 metafile=/.+/")):
            try:
                mtime = int(Path(i.metafile).stat().st_mtime)
                if self.args.dry_run:
                    dt = datetime.fromtimestamp(mtime)
                    self.log.info(
                        "Would set %s tm_loaded to %s from metafile %s",
                        i.hash,
                        dt,
                        i.metafile,
                    )
                else:
                    i.rpc_call("d.custom.set", ["tm_loaded", str(mtime)])
                    i.flush()
            except Exception as e:
                self.log.error("Could not set tm_loaded for %s: %s", i.hash, e)
        for i in engine.view("main", matching.create_matcher("loaded=0 path=/.+/")):
            try:
                mtime = int(Path(i.path).stat().st_mtime)
                if self.args.dry_run:
                    dt = datetime.fromtimestamp(mtime)
                    self.log.info(
                        "Would set %s tm_loaded to %s from path %s", i.hash, dt, i.path
                    )
                else:
                    i.rpc_call("d.custom.set", ["tm_loaded", str(mtime)])
                    i.flush()
            except Exception as e:
                self.log.error("Could not set tm_loaded for %s: %s", i.hash, e)
        for i in engine.view(
            "main", matching.create_matcher("completed=0 is_complete=yes path=/.+/")
        ):
            try:
                mtime = int(Path(i.path).stat().st_mtime)
                if self.args.dry_run:
                    dt = datetime.fromtimestamp(mtime)
                    self.log.info(
                        "Would set %s tm_completed to %s from path %s",
                        i.hash,
                        dt,
                        i.path,
                    )
                else:
                    i.rpc_call("d.custom.set", ["tm_completed", str(mtime)])
                    i.flush()
            except Exception as e:
                self.log.error("Could not set tm_loaded for %s: %s", i.hash, e)

    def create_config(self):
        """Create a configuration file"""
        config_path = Path("~/.config/pyrosimple/config.toml").expanduser()
        if config_path.exists():
            self.log.info(
                "Pyrosimple config path %s already exists, not overwriting", config_path
            )
        else:
            self.log.info("Creating pyrosimple config file '%s'", config_path)
            with config_path.open("wb") as fh:
                tomli_w.dump(pyrosimple.config.settings.to_dict(), fh)

    def create_rtorrent_rc(self):
        """Create a rtorrent.rc file"""
        rtorrent_rc_path = Path(pyrosimple.config.settings.RTORRENT_RC).expanduser()
        if rtorrent_rc_path.exists():
            self.log.info(
                "rtorrent.rc path %s already exists, not overwriting", rtorrent_rc_path
            )
        else:
            self.log.info("Creating rtorrent.rc at '%s'", rtorrent_rc_path)
            home = str(Path("~").expanduser())
            with rtorrent_rc_path.open("w", encoding="utf-8") as fh:
                fh.write(
                    importlib.resources.open_text("pyrosimple.data", "full-example.rc")
                    .read()
                    .replace("/home/USERNAME", home)
                )

    def config(self):
        """Handle the config subcommand"""
        if self.args.dump_rc:
            self.dump_rc()
        if self.args.create_config:
            self.create_config()
        if self.args.create_rtorrent_rc:
            self.create_rtorrent_rc()
        if self.args.check:
            try:
                config.autoload_scgi_url()
            except Exception:
                self.log.error("Error loading SCGI URL:")
                raise
            self.log.debug("Loaded SCGI URL successfully")
            try:
                pyrosimple.connect().open()
            except ConnectionRefusedError:
                self.log.error(
                    "SCGI URL '%s' found, but rTorrent may not be running!",
                    config.autoload_scgi_url(),
                )
                raise
            self.log.info("Connected to rTorrent successfully")

    def mainloop(self):
        self.args = self.parser.parse_args()
        if self.args.func is None:
            self.parser.print_help()
            return
        self.args.func()


def run():  # pragma: no cover
    """The entry point."""
    AdminTool().run()


if __name__ == "__main__":
    run()
