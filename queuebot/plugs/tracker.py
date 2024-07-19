#!/usr/bin/python
from __future__ import print_function
import threading
import traceback
import xmlrpc.client as xmlrpclib


class TrackerScanner(threading.Thread):
    notices = list()

    def run(self):
        try:
            self.notices = list()

            # In verbose mode, show the current content of the queue
            if self.verbose and self.queue not in self.tracker_state:
                self.tracker_state[self.queue] = set()

            milestones = [milestone for milestone in
                          self.drupal.qatracker.milestones.get_list([0])
                          if milestone['notify'] == "1"
                          and 'Touch' not in milestone['title']]

            products = {}
            for product in self.drupal.qatracker.products.get_list([0]):
                products[product['id']] = product

            new_list = set()
            for milestone in milestones:
                for build in self.drupal.qatracker.builds.get_list(
                        int(milestone['id']), [0, 1, 4]):
                    build_milestone = milestone['title']
                    build_product = products[build['productid']]['title']
                    build_version = build['version']
                    build_status = build['status_string']
                    new_list.add("%s;%s;%s;%s" % (build_milestone,
                                                  build_product,
                                                  build_version,
                                                  build_status))

            if self.queue in self.tracker_state:
                build_products = [";".join(build.split(';')[0:2])
                                  for build in self.tracker_state[self.queue]]
                new_build_products = [";".join(build.split(';')[0:2])
                                      for build in new_list]

                # Print removed images
                for build in self.tracker_state[self.queue] - new_list:
                    build_milestone, build_product, build_version, \
                        build_status = build.split(';')

                    if "%s;%s" % (build_milestone, build_product) \
                            in new_build_products:
                        continue

                    # Post to the channels. Don't mark all the records
                    # as removed when we remove a milestone
                    skip = False
                    for milestone in milestones:
                        if build_milestone == milestone['title']:
                            skip = True
                            break
                    else:
                        skip = True

                    if not skip:
                        self.notices.append(("%s: %s [%s] has been removed" % (
                            self.queue, build_product, build_milestone),
                            ("tracker",)))

                # Print other changes and deal with cases where a released
                # milestone is moved back to testing
                if len(new_list - self.tracker_state[self.queue]) > 25:
                    self.notices.append((
                        "%s: %s entries have been "
                        "added, updated or disabled" % (
                            self.queue, len(new_list -
                                            self.tracker_state[self.queue])),
                        ("tracker",)))
                elif len(self.tracker_state[self.queue] - new_list) > 25:
                    self.notices.append((
                        "%s: %s entries have been "
                        "added, updated or disabled" % (
                            self.queue,
                            len(self.tracker_state[self.queue] - new_list)),
                        ("tracker",)))
                else:
                    for build in sorted(
                            new_list - self.tracker_state[self.queue]):

                        build_milestone, build_product, build_version, \
                            build_status = build.split(';')

                        if "%s;%s" % (build_milestone, build_product) \
                                in build_products:
                            if build_status == "Re-building":
                                self.notices.append((
                                    "%s: %s [%s] has been disabled" % (
                                        self.queue, build_product,
                                        build_milestone), ("tracker",)))
                            elif build_status == "Ready":
                                self.notices.append((
                                    "%s: %s [%s] has been marked as ready" % (
                                        self.queue, build_product,
                                        build_milestone), ("tracker",)))
                            else:
                                self.notices.append((
                                    "%s: %s [%s] has been updated (%s)" % (
                                        self.queue, build_product,
                                        build_milestone, build_version),
                                    ("tracker",)))
                        else:
                            self.notices.append((
                                "%s: %s [%s] (%s) has been added" % (
                                    self.queue, build_product, build_milestone,
                                    build_version), ("tracker",)))

            self.tracker_state[self.queue] = new_list
        except:
            # We don't want the bot to crash when LP fails
            traceback.print_exc()


class Tracker():
    tracker_state = dict()
    scanner = TrackerScanner()
    name = "tracker"
    queue = ""

    def __init__(self, queue, verbose=False):
        self.queue = queue
        self.verbose = verbose

        # Setup ISO tracker
        self.drupal = xmlrpclib.ServerProxy(
            "https://iso.qa.ubuntu.com/xmlrpc.php")

        self.spawn_scanner()

    def spawn_scanner(self):
        if self.scanner.is_alive():
            raise Exception("Scanner is already running")

        self.scanner = TrackerScanner()
        self.scanner.tracker_state = self.tracker_state
        self.scanner.verbose = self.verbose
        self.scanner.drupal = self.drupal
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
