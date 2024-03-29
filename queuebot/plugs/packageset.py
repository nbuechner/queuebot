#!/usr/bin/python
from __future__ import print_function

import traceback
import threading
from launchpadlib.launchpad import Launchpad


class PackagesetScanner(threading.Thread):
    notices = list()

    def run(self):
        try:
            # Authenticated login to Launchpad
            self.lp = Launchpad.login_anonymously(
                'maubot-queuebot', 'production',
                launchpadlib_dir="/tmp/queuebot-%s/" % self.queue)

            self.notices = list()

            ubuntu = self.lp.distributions['ubuntu']
            ubuntu_series = [series for series in ubuntu.series
                             if series.active]

            # In verbose mode, show the current content of the queue
            if self.verbose and self.queue not in self.queue_state:
                self.queue_state[self.queue] = set()

            # Get the content of the current queue
            new_list = set()
            for series in ubuntu_series:
                for pkgset in self.lp.packagesets.getBySeries(
                        distroseries=series):
                    for pkg in list(pkgset.getSourcesIncluded()):
                        new_list.add(";".join([
                            series.self_link,
                            series.name,
                            pkgset.name,
                            pkg
                        ]))

            if self.queue in self.queue_state:
                if len(new_list - self.queue_state[self.queue]) > 25:
                    self.notices.append(("%s: %s entries have been"
                                         " added or removed" %
                                         (self.queue,
                                          len(new_list -
                                              self.queue_state[self.queue])),
                                         ['packageset']))
                elif len(self.queue_state[self.queue] - new_list) > 25:
                    self.notices.append(("%s: %s entries have been"
                                         " added or removed" %
                                         (self.queue,
                                          len(self.queue_state[self.queue] -
                                              new_list)),
                                         ['packageset']))
                else:
                    # Print removed packages
                    for pkg in sorted(self.queue_state[self.queue] - new_list):
                        pkg_seriesurl, pkg_series, pkg_set, \
                            pkg_name = pkg.split(';')

                        self.notices.append(("%s: Removed %s from %s in %s" % (
                            self.queue, pkg_name, pkg_set, pkg_series),
                            ['packageset']))

                    # Print added packages
                    for pkg in sorted(new_list - self.queue_state[self.queue]):
                        pkg_seriesurl, pkg_series, pkg_set, \
                            pkg_name = pkg.split(';')

                        self.notices.append(("%s: Added %s to %s in %s" % (
                            self.queue, pkg_name, pkg_set, pkg_series),
                            ['packageset']))

            self.queue_state[self.queue] = new_list
        except:
            # We don't want the bot to crash when LP fails
            traceback.print_exc()


class Packageset():
    queue_state = dict()
    scanner = PackagesetScanner()
    name = "packageset"
    queue = ""

    def __init__(self, queue, verbose=False):
        self.queue = queue
        self.verbose = verbose
        self.spawn_scanner()

    def spawn_scanner(self):
        if self.scanner.is_alive():
            raise Exception("Scanner is already running")

        self.scanner = PackagesetScanner()
        self.scanner.queue_state = self.queue_state
        self.scanner.verbose = self.verbose
        self.scanner.queue = self.queue
        self.scanner.start()

    def update(self):
        if self.scanner.is_alive():
            return False

        # Get the result from the thread
        notices = list(self.scanner.notices)

        # Spawn a new insance of the monitoring thread
        self.spawn_scanner()

        return notices
