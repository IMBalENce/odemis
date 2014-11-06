#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 3 Nov 2014

@author: Éric Piel

Copyright © 2014 Éric Piel, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 2 as published by the Free Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Odemis. If not, see http://www.gnu.org/licenses/.
'''

# Start the gui and back-end in any case
# Accepts one argument: the microscope model file, which overrides the MODEL
# default value.
# For now, only works for Ubuntu

from Pyro4.errors import CommunicationError
import logging
from odemis import model
from odemis.util import driver
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import wx
import wx.lib.dialogs


class Starter(object):
    def __init__(self, configfile):
        self._configfile = configfile
        self.config = {} # str -> str
        # Set some default values (that can be overridden by the config file)
        self.config["LOGLEVEL"] = "1"
        self.config["TERMINAL"] = "/usr/bin/gnome-terminal"
        self.parse_config()

        # For displaying wx windows
        print "Creating app"
        self.wxapp = wx.App()
        self.wxframe = wx.Frame(None)

        # Warning: wx will crash if pynotify has been loaded before creating the
        # wx.App (probably due to bad interaction with GTK).
        # That's why we only import it here.
        import pynotify
        pynotify.init("Odemis")
        self._notif = pynotify.Notification("")

        # For listening to component states
        self._mic = None
        self._comp_state = {} # str -> state
        self._backend_done = threading.Event()

        # For reacting to SIGNINT (only once)
        self._main_thread = threading.current_thread()

    def _add_var(self, var, content):
        """
        Add one variable to the .config, handling substitution
        """
        # variable substitution
        m = re.search(r"(\$\w+)", content)
        while m:
            subvar = m.group(1)[1:]
            # First try to use a already known variable, and fallback to environ
            try:
                subcont = self.config[subvar]
            except KeyError:
                try:
                    subcont = os.environ[subvar]
                except KeyError:
                    logging.warning("Failed to find variable %s", subvar)
                    subcont = ""
            # substitute (might do several at a time, but it's fine)
            content = content.replace(m.group(1), subcont)
            m = re.search(r"(\$\w+)", content)

        self.config[var] = content

    def parse_config(self):
        """
        Parse /etc/odemis.conf, which was originally designed to be parsed as
        a bash script. So each line looks like:
        VAR=$VAR2/log
        It updates .config
        """
        f = open(self._configfile)
        for line in shlex.split(f, comments=True):
            tokens = line.split("=")
            if len(tokens) != 2:
                logging.warning("Can't parse '%s', skipping the line", line)
            else:
                self._add_var(tokens[0], tokens[1])

    def start_backend(self, modelfile):
        """
        Start the backend, and returns when it's fully instantiated or failed
        It will display a simple window indicating the progress
        """
        self._notif.update("Starting Odemis back-end", "", "dialog-info")
        self._notif.show()

        # install cgroup, for memory protection
        if (not os.path.exists("/sys/fs/cgroup/memory/odemisd")
            and os.path.exists("/usr/bin/cgcreate")):
            logging.info("Creating cgroup")
            subprocess.call(["sudo", "/usr/bin/cgcreate", "-a", ":odemis", "-g", "memory:odemisd"])

        logging.info("Starting back-end...")

        # odemisd likes to start as root to be able to create /var/run files, but then
        # drop its privileges to the odemis group
        # use sudo background mode to be sure it won't be killed at the end
        error = subprocess.call(["sudo", "-b", "odemisd", "--daemonize",
                               "--log-level", self.config["LOGLEVEL"],
                               "--log-target", self.config["LOGFILE"],
                               modelfile])

        # If it immediately fails, it's easy
        if error != 0:
            self._notif.update("Odemis back-end failed to start",
                               "For more information type odemis-start in a terminal.",
                               "dialog-warning")
            self._notif.show()
            raise ValueError("Starting back-end failed")

    def _on_sigint(self, signum, frame):
        # TODO: ensure this is only processed by the main thread
        if threading.current_thread() == self._main_thread:
            logging.warning("Received signal %d: stopping", signum)
            raise KeyboardInterrupt("Received signal %d" % signum)
        else:
            logging.info("Skipping signal %d in sub-thread", signum)


    # TODO: catch SIGINT and stop backend
    def wait_backend_is_ready(self):
        """
        Blocks until the back-end is fully ready (all the components are ready)
        raise:
            IOError: if the back-end eventually fails to start
        """

        # Get a connection to the back-end
        end_time = time.time() + 5 # 5s max to start the backend
        while self._mic is None:
            try:
                backend = model.getContainer(model.BACKEND_NAME, validate=False)
                self._mic = backend.getRoot()
            except (IOError, CommunicationError):
                if time.time() > end_time:
                    raise IOError("Back-end failed to start")
                else:
                    logging.debug("Waiting a bit more for the backend to appear")
                    time.sleep(1)

        # In theory Python raise KeyboardInterrupt on SIGINT, which is what we
        # need. But that doesn't happen if in a wait(), and in addition, when
        # there are several threads, only one of them receives the exception.
        signal.signal(signal.SIGINT, self._on_sigint)

        # TODO: create a window with the list of the components
        self._mic.ghosts.subscribe(self._on_ghosts, init=True)

        # Wait until there is no more ghosts
        try:
            while True:
                # Sleep a bit
                self._backend_done.wait(1)
                try:
                    backend.ping()
                except (IOError, CommunicationError):
                    raise IOError("Back-end failed to fully instantiate")
                if self._backend_done.is_set():
                    logging.debug("Back-end appears ready")
                    self._notif.update("Odemis back-end successfully started",
                                       "Graphical interface will now start.",
                                       "dialog-info")
                    self._notif.show()
                    return
        except KeyboardInterrupt:
            logging.info("Stopping the backend")
            backend.terminate()
            raise
        finally:
            # TODO: close the window
            pass

    def _on_ghosts(self, ghosts):
        """
        Called when the .ghosts changes
        """
        # The components running fine
        for c in self._mic.alive.value:
            state = c.state.value
            if self._comp_state.get(c.name) != state:
                self._comp_state[c.name] = state
                print "Component %s: %s" % (c.name, state)

        # Now the defective ones
        for cname, state in ghosts.items():
            if isinstance(state, Exception):
                # Exceptions are different even if just a copy
                statecmp = str(state)
            else:
                statecmp = state
            if self._comp_state.get(cname) != statecmp:
                self._comp_state[cname] = statecmp
                print "Component %s: %s" % (cname, statecmp)

        # No more ghosts, means all hardware is ready
        if not ghosts:
            self._backend_done.set()

    def display_backend_log(self):
        f = open(self.config["LOGFILE"], "r")
        lines = f.readlines()

        # Start at the beginning of the latest log
        for i, l in enumerate(reversed(lines)):
            if "Starting Odemis back-end" in l:
                startl = -(i + 1)
                break
        else:
            startl = 0

        msg = "\n".join(lines[startl:])


        # TODO: display directly the end of the log
        # At least, skip everything not related to this last run
        dlg = wx.lib.dialogs.ScrolledMessageDialog(self.wxframe, msg,
                                                   "Log message of Odemis back-end")
        dlg.CenterOnScreen()
        dlg.ShowModal()
        dlg.Destroy()

    def start_gui(self):
        """
        Starts the GUI and immediately return
        """
        subprocess.check_call(["odemis-gui", "--log-level", self.config["LOGLEVEL"]])


def main(args):
    """
    Handles the command line arguments
    args is the list of arguments passed
    return (int): value to return to the OS as program exit code
    """
    starter = Starter("/etc/odemis.conf")

    # Use the loglevel for ourselves first
    try:
        loglevel = int(starter.config["LOGLEVEL"])
    except ValueError:
        loglevel = 1
        starter.config["LOGLEVEL"] = "%d" % loglevel
    logging.getLogger().setLevel(loglevel)

#     pyrolog = logging.getLogger("Pyro4")
#     pyrolog.setLevel(min(pyrolog.getEffectiveLevel(), logging.DEBUG))

    # Updates the python path if requested
    if "PYTHONPATH" in starter.config:
        logging.debug("PYTHONPATH set to '%s'", starter.config["PYTHONPATH"])
        os.environ["PYTHONPATH"] = starter.config["PYTHONPATH"]

    try:
        if len(args) > 2:
            raise ValueError("Only 0 or 1 argument accepted")
        elif len(args) == 2:
            modelfile = args[1]
        else:
            modelfile = starter.config["MODEL"]


        # TODO: just bring the focus?
        # Kill GUI if an instance is already there
        # TODO: use psutil.process_iter() for this
        gui_killed = subprocess.call(["/usr/bin/pkill", "-f", starter.config["GUI"]])
        if gui_killed == 0:
            logging.info("Found the GUI still running, killing it first...")

        status = driver.get_backend_status()
        # TODO: if backend running but with a different model, also restart it
        if status == driver.BACKEND_DEAD:
            logging.warning("Back-end is not responding, will restart it...")
            subprocess.call(["/usr/bin/pkill", "-f", starter.config["BACKEND"]])
            time.sleep(1)

        try:
            if status in (driver.BACKEND_DEAD, driver.BACKEND_STOPPED):
                starter.start_backend(modelfile)
            if status in (driver.BACKEND_DEAD, driver.BACKEND_STOPPED, driver.BACKEND_STARTING):
                starter.wait_backend_is_ready()
            else:
                logging.debug("Back-end already started, so not starting again")
        except IOError:
            starter.display_backend_log()
            raise

        starter.start_gui()

    except ValueError as exp:
        logging.error("%s", exp)
        return 127
    except IOError as exp:
        logging.error("%s", exp)
        return 129
    except Exception:
        logging.exception("Unexpected error while performing action.")
        return 130

    return 0

if __name__ == '__main__':
    ret = main(sys.argv)
    exit(ret)
