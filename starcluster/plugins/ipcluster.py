"""
A starcluster plugin for running an IPython cluster using SGE
(requires IPython 0.11+pyzmq or 0.10+twisted)

See ipythondev plugin for installing git master IPython and its dependencies
"""
import os
import time
import posixpath

from starcluster import static
from starcluster import spinner
from starcluster.utils import print_timing
from starcluster.clustersetup import ClusterSetup

from starcluster.logger import log

IPCLUSTER_CACHE = os.path.join(static.STARCLUSTER_CFG_DIR, 'ipcluster')

STARTED_MSG_10 = """\
IPCluster has been started on %(cluster)s for user '%(user)s'.

To use IPCluster (0.10.*) you first need to login to the master node of the
cluster as '%(user)s':

    $ starcluster sshmaster %(cluster)s -u %(user)s

Once you've logged in the first step is to launch IPython and load the parallel
client:

    $ ipython
    [~]> from IPython.kernel import client
    [~]> mec = client.MultiEngineClient()
    [~]> mec.get_ids()
    [0, 1, 2, 3]

This shows that we have 4 engines running on our cluster. Below is an example
of how to run a parallel map across all nodes in the cluster using the
MultiEngineClient interface:

    [~]> print mec.map(lambda x:x**30, range(8))
    [0,
     1,
     1073741824,
     205891132094649L,
     1152921504606846976L,
     931322574615478515625L,
     221073919720733357899776L,
     22539340290692258087863249L]

See the IPython 0.10.* parallel docs for more details
(http://ipython.org/ipython-doc/rel-0.10.2/html/parallel)
"""


class IPCluster10(ClusterSetup):
    """
    Starts an IPCluster (0.10.*) on StarCluster
    """
    cluster_file = '/etc/clusterfile.py'
    log_file = '/var/log/ipcluster.log'

    def _create_cluster_file(self, master, nodes):
        engines = {}
        for node in nodes:
            engines[node.alias] = node.num_processors
        cfile = 'send_furl = True\n'
        cfile += 'engines = %s\n' % engines
        f = master.ssh.remote_file(self.cluster_file, 'w')
        f.write(cfile)
        f.close()

    def run(self, nodes, master, user, user_shell, volumes):
        self._create_cluster_file(master, nodes)
        log.info("Starting ipcluster...")
        master.ssh.execute(
            "su - %s -c 'screen -d -m ipcluster ssh --clusterfile %s'" %
            (user, self.cluster_file))
        log.info(STARTED_MSG_10 % dict(cluster=master.parent_cluster,
                                       user=user))

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        log.info("Adding %s to ipcluster" % node.alias)
        self._create_cluster_file(master, nodes)
        user_home = node.getpwnam(user).pw_dir
        furl_file = posixpath.join(user_home, '.ipython', 'security',
                                   'ipcontroller-engine.furl')
        node.ssh.execute(
            "su - %s -c 'screen -d -m ipengine --furl-file %s'" %
            (user, furl_file))

    def on_remove_node(self, node, nodes, master, user, user_shell, volumes):
        log.info("Removing %s from ipcluster" % node.alias)
        less_nodes = filter(lambda x: x.id != node.id, nodes)
        self._create_cluster_file(master, less_nodes)
        node.ssh.execute('pkill ipengine')


STARTED_MSG_11 = """\
IPCluster has been started on %(cluster)s for user '%(user)s'.

To use IPCluster log in to the master node as '%(user)s', create a parallel
client, and run some parallel tasks on the cluster:

    $ starcluster sshmaster %(cluster)s -u %(user)s
    $ ipython
    [~]> from IPython.parallel import Client
    [~]> rc = Client(packer='pickle')
    [~]> view = rc[:]
    [~]> results = view.map_async(lambda x: x**30, range(4))
    [~]> print results.get()
    [0,
     1,
     1073741824,
     205891132094649L]

Alternatively, if IPython 0.11+ is installed locally, you can have StarCluster
configure an interactive parallel IPython session automatically for you:

    $ starcluster shell --ipcluster=%(cluster)s

This will start IPython on your local computer and automatically create a
parallel client and a view of the entire remote cluster in variables 'ipclient'
and 'ipview' respectively:

    $ starcluster shell --ipcluster=%(cluster)s
    [~]> ipclient.ids
    [0, 1, 2, 3]
    [~]> res = ipview.map_async(lambda x: x**30, range(8))
    [~]> print res.get()

See the IPCluster plugin doc for more details:
http://web.mit.edu/starcluster/docs/latest/plugins/ipython.html
"""


class IPCluster11(ClusterSetup):
    """
    Start an IPython (0.11) cluster
    """

    def _write_config(self, master, profile_dir):
        """
        Create cluster config
        """
        log.info("Writing IPython cluster config files")
        master.ssh.execute('ipython profile create')
        f = master.ssh.remote_file('%s/ipcontroller_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "c.HubFactory.ip='%s'" % master.private_ip_address,
            "c.IPControllerApp.ssh_server='%s'" % master.public_dns_name,
            # "c.Application.log_level = 'DEBUG'",
            "",
        ]))
        f.close()

        f = master.ssh.remote_file('%s/ipcluster_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "c.IPClusterStart.controller_launcher_class=" +
            "'SGEControllerLauncher'",
            # restrict controller to master node:
            "c.SGEControllerLauncher.queue='all.q@master'",
            "c.IPClusterEngines.engine_launcher_class='SGEEngineSetLauncher'",
            # "c.Application.log_level = 'DEBUG'",
            "",
        ]))
        f.close()
        f = master.ssh.remote_file('%s/ipengine_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "c.EngineFactory.timeout = 10",
            # Engines should wait a while for url files to arrive,
            # in case Controller takes a bit to start:
            "c.IPEngineApp.wait_for_url_file = 30",
            # "c.Application.log_level = 'DEBUG'",
            "",
        ]))
        f.close()
        f = master.ssh.remote_file('%s/ipython_config.py' % profile_dir)
        f.write('\n'.join([
            "c = get_config()",
            "try:",
            "    import msgpack",
            "except ImportError:",
            # use pickle if msgpack is unavailable
            "    c.Session.packer='pickle'",
            "else:",
            # use msgpack if we can, because it's fast
            "    c.Session.packer='msgpack.packb'",
            "    c.Session.unpacker='msgpack.unpackb'",
            "c.EngineFactory.timeout = 10",
            # Engines should wait a while for url files to arrive,
            # in case Controller takes a bit to start via SGE
            "c.IPEngineApp.wait_for_url_file = 30",
            # "c.Application.log_level = 'DEBUG'",
            "",
        ]))
        f.close()

    def _start_cluster(self, master, n, profile_dir):
        log.info("Starting IPython cluster with %i engines" % n)
        # cleanup existing connection files, to prevent their use
        master.ssh.execute("rm -f %s/security/*.json" % profile_dir)
        master.ssh.execute("ipcluster start --n=%i --delay=5 --daemonize" % n,
                           source_profile=True)
        # wait for JSON file to exist
        json = '%s/security/ipcontroller-client.json' % profile_dir
        log.info("Waiting for JSON connector file...",
                 extra=dict(__nonewline__=True))
        s = spinner.Spinner()
        s.start()
        while not master.ssh.isfile(json):
            time.sleep(1)
        s.stop()
        # retrieve JSON connection info
        if not os.path.isdir(IPCLUSTER_CACHE):
            log.info("Creating IPCluster cache directory: %s" %
                     IPCLUSTER_CACHE)
            os.makedirs(IPCLUSTER_CACHE)
        local_json = os.path.join(IPCLUSTER_CACHE,
                                  '%s-%s.json' % (master.parent_cluster,
                                                  master.region.name))
        log.info("Saving JSON connector file to '%s'" %
                 os.path.abspath(local_json))
        master.ssh.get(json, local_json)
        return local_json

    @print_timing("IPCluster")
    def run(self, nodes, master, user, user_shell, volumes):
        n = sum([node.num_processors for node in nodes]) - 1
        user_home = node.getpwnam(user).pw_dir
        profile_dir = posixpath.join(user_home, '.ipython', 'profile_default')
        master.ssh.switch_user(user)
        self._write_config(master, profile_dir)
        cfile = self._start_cluster(master, n, profile_dir)
        log.info(STARTED_MSG_11 % dict(cluster=master.parent_cluster,
                                       user=user, connector_file=cfile,
                                       key_location=master.key_location))
        master.ssh.switch_user('root')

    def _stop_cluster(self, master, user):
        master.ssh.execute("pkill -f ipengineapp.py")
        master.ssh.execute("pkill -f ipcontrollerapp.py")

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        n = node.num_processors
        log.info("Adding %i engines on %s to ipcluster" % (n, node.alias))
        node.ssh.execute("ipcluster engines --n=%i --daemonize" % n,
                         source_profile=True)


class IPCluster(ClusterSetup):

    def _get_ipy_version(self, node):
        version_cmd = "python -c 'import IPython; print IPython.__version__'"
        return node.ssh.execute(version_cmd)[0]

    def _get_ipcluster_plugin(self, node):
        ipyversion = self._get_ipy_version(node)
        if ipyversion < '0.11':
            if not ipyversion.startswith('0.10'):
                log.warn("Trying unsupported IPython version %s" % ipyversion)
            return IPCluster10()
        else:
            return IPCluster11()

    def run(self, nodes, master, user, user_shell, volumes):
        plug = self._get_ipcluster_plugin(master)
        plug.run(nodes, master, user, user_shell, volumes)

    def on_add_node(self, node, nodes, master, user, user_shell, volumes):
        plug = self._get_ipcluster_plugin(master)
        plug.on_add_node(node, nodes, master, user, user_shell, volumes)

    def on_remove_node(self, node, nodes, master, user, user_shell, volumes):
        plug = self._get_ipcluster_plugin(master)
        plug.on_remove_node(node, nodes, master, user, user_shell, volumes)


class IPClusterStop(ClusterSetup):

    def run(self, nodes, master, user, user_shell, volumes):
        log.info("Shutting down IPython cluster")
        master.ssh.switch_user(user)
        master.ssh.execute("ipcluster stop", source_profile=True)
        time.sleep(2)
        master.ssh.execute("pkill -f ipcontrollerapp.py",
                           ignore_exit_status=True)
        for node in nodes:
            master.ssh.execute("pkill -f ipengineapp.py",
                               ignore_exit_status=True)
        master.ssh.switch_user('root')
