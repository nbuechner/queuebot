#!/usr/bin/python
from __future__ import print_function

import traceback
import threading
from launchpadlib.launchpad import Launchpad

class QueueScanner(threading.Thread):
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
                for pkg in series.getPackageUploads(status=self.queue):
                    # Split the different sub-packages
                    all_name = pkg.display_name.split(', ')
                    all_arch = pkg.display_arches.split(', ')
                    all_pkg = []
                    for name in all_name:
                        all_pkg.append((name, all_arch[all_name.index(name)]))

                    for (name, arch) in all_pkg:
                        if name.startswith('language-pack-'):
                            continue

                        if name.startswith('kde-l10n-'):
                            continue

                        if arch.startswith('raw-'):
                            continue

                        if arch == 'uefi' or arch == 'signing':
                            continue

                        new_list.add(";".join([
                            series.self_link,
                            "%s-%s" % (series.name.lower(),
                                       pkg.pocket.lower()),
                            name,
                            pkg.display_version,
                            arch,
                            pkg.archive.name,
                            pkg.self_link,
                        ]))

            if self.queue in self.queue_state:
                # Print removed packages
                for pkg in sorted(self.queue_state[self.queue] - new_list):
                    pkg_seriesurl, pkg_pocket, pkg_name, pkg_version, \
                        pkg_arch, pkg_archive, pkg_self = pkg.split(';')
                    pkg_status = self.lp.load(pkg_self).status
                    if pkg_status == "Rejected":
                        status = "rejected"
                    elif pkg_status in ("Accepted", "Done"):
                        status = "accepted"
                    else:
                        print("Impossible package status: %s "
                              "(%s, %s, %s, %s, %s)" %
                              (pkg_status, self.queue, pkg_name,
                               pkg_arch, pkg_pocket, pkg_version))
                        continue

                    mute = (
                        "queue;%s" % (pkg_pocket),
                        "queue;%s" % (self.queue.lower()),
                        "queue;%s;%s" % (pkg_pocket, self.queue.lower()),
                        "queue;%s;%s" % (self.queue.lower(), pkg_pocket)
                        )
                    self.notices.append(("%s: %s %s [%s] (%s) [%s]" % (
                        self.queue, status, pkg_name, pkg_arch,
                        pkg_pocket, pkg_version), mute))

                # Print added packages
                for pkg in sorted(new_list - self.queue_state[self.queue]):
                    pkg_seriesurl, pkg_pocket, pkg_name, pkg_version, \
                        pkg_arch, pkg_archive, pkg_self = pkg.split(';')
                    pkg_series = self.lp.load(pkg_seriesurl)

                    # Try to get some more data by looking at
                    # the current archive
                    current_component = 'none'
                    current_version = 'none'
                    current_pkgsets = set()
                    for archive in ubuntu.archives:
                        current_pkg = archive.getPublishedSources(
                            source_name=pkg_name, status="Published",
                            distro_series=pkg_series, exact_match=True)
                        if list(current_pkg):
                            current_component = current_pkg[0].component_name
                            current_version = \
                                current_pkg[0].source_package_version
                            break

                    for pkgset in self.lp.packagesets.setsIncludingSource(
                            distroseries=pkg_series,
                            sourcepackagename=pkg_name):
                        current_pkgsets.add(pkgset.name)

                    # Prepare the packageset list
                    if current_pkgsets:
                        pkg_pkgsets = ", ".join(sorted(current_pkgsets))
                    else:
                        pkg_pkgsets = "no packageset"

                    # Post the mssage to the channel
                    message = ""
                    if self.queue == 'New':
                        if pkg_arch == "source":
                            message = "%s source: %s (%s/%s) [%s]" % (
                                self.queue, pkg_name, pkg_pocket,
                                pkg_archive, pkg_version)
                        elif pkg_arch == "sync":
                            message = "%s sync: %s (%s/%s) [%s]" % (
                                self.queue, pkg_name, pkg_pocket,
                                pkg_archive, pkg_version)
                        else:
                            message = "%s binary: %s [%s] (%s/%s) [%s] (%s)" \
                                % (self.queue, pkg_name, pkg_arch,
                                    pkg_pocket, current_component,
                                    pkg_version, pkg_pkgsets)
                    else:
                        message = "%s: %s (%s/%s) [%s => %s] (%s)" % (
                            self.queue, pkg_name, pkg_pocket,
                            current_component, current_version,
                            pkg_version, pkg_pkgsets)

                        if pkg_arch == "sync":
                            message += " (sync)"

                    mute = (
                        "queue;%s" % (pkg_pocket),
                        "queue;%s" % (self.queue.lower()),
                        "queue;%s;%s" % (pkg_pocket, self.queue.lower()),
                        "queue;%s;%s" % (self.queue.lower(), pkg_pocket)
                        )
                    self.notices.append((message, mute))
            self.queue_state[self.queue] = new_list
        except:
            # We don't want the bot to crash when LP fails
            traceback.print_exc()


class Queue():
    queue_state = dict()
    scanner = QueueScanner()
    name = "queue"
    queue = ""

    def __init__(self, queue, verbose=False):
        self.queue = queue
        self.verbose = verbose
        self.spawn_scanner()

    def spawn_scanner(self):
        if self.scanner.is_alive():
            raise Exception("Scanner is already running")

        self.scanner = QueueScanner()
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
