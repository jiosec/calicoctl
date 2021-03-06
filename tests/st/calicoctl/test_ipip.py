# Copyright (c) 2015-2016 Tigera, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import re
import subprocess

from netaddr import IPAddress, IPNetwork
from tests.st.test_base import TestBase
from tests.st.utils.docker_host import DockerHost, CLUSTER_STORE_DOCKER_OPTIONS
from tests.st.utils.constants import DEFAULT_IPV4_POOL_CIDR
from tests.st.utils.utils import retry_until_success
from time import sleep

"""
Test calico IPIP behaviour.
"""

class TestIPIP(TestBase):
    def tearDown(self):
        self.remove_tunl_ip()

    def test_ipip(self):
        """
        Test IPIP routing with the different IPIP modes.

        This test modifies the working IPIP mode of the pool and monitors the
        traffic flow to ensure it either is or is not going over the IPIP
        tunnel as expected.
        """
        with DockerHost('host1',
                        additional_docker_options=CLUSTER_STORE_DOCKER_OPTIONS,
                        start_calico=False) as host1, \
             DockerHost('host2',
                        additional_docker_options=CLUSTER_STORE_DOCKER_OPTIONS,
                        start_calico=False) as host2:

            # Autodetect the IP addresses - this should ensure the subnet is
            # correctly configured.
            host1.start_calico_node("--ip=autodetect")
            host2.start_calico_node("--ip=autodetect")

            # Create a network and a workload on each host.
            network1 = host1.create_network("subnet1")
            workload_host1 = host1.create_workload("workload1",
                                                   network=network1)
            workload_host2 = host2.create_workload("workload2",
                                                   network=network1)

            # Allow network to converge
            self.assert_true(
                workload_host1.check_can_ping(workload_host2.ip, retries=10))

            # Check connectivity in both directions
            self.assert_ip_connectivity(workload_list=[workload_host1,
                                                       workload_host2],
                                        ip_pass_list=[workload_host1.ip,
                                                      workload_host2.ip])

            # Note in the following we are making a number of configuration
            # changes and testing whether or not IPIP is being used.
            # The order of tests is deliberately chosen to flip between IPIP
            # and no IPIP because it is easier to look for a change of state
            # than to look for state remaining the same.

            # Turn on IPIP, default mode (which is always use IPIP), and check
            # IPIP tunnel is being used.
            self.pool_action(host1, "replace", DEFAULT_IPV4_POOL_CIDR, True)
            self.assert_ipip_routing(host1, workload_host1, workload_host2,
                                     True)

            # Turn off IPIP and check IPIP tunnel is not being used.
            self.pool_action(host1, "replace", DEFAULT_IPV4_POOL_CIDR, False)
            self.assert_ipip_routing(host1, workload_host1, workload_host2,
                                     False)

            # Turn on IPIP mode "always", and check IPIP tunnel is being used.
            self.pool_action(host1, "replace", DEFAULT_IPV4_POOL_CIDR, True,
                             ipip_mode="always")
            self.assert_ipip_routing(host1, workload_host1, workload_host2,
                                     True)

            # Turn on IPIP mode "cross-subnet", since both hosts will be on the
            # same subnet, IPIP should not be used.
            self.pool_action(host1, "replace", DEFAULT_IPV4_POOL_CIDR, True,
                             ipip_mode="cross-subnet")
            self.assert_ipip_routing(host1, workload_host1, workload_host2,
                                     False)

            # Set the BGP subnet on both node resources to be a /32.  This will
            # fool Calico into thinking they are on different subnets.  IPIP
            # routing should be used.
            self.pool_action(host1, "replace", DEFAULT_IPV4_POOL_CIDR, True,
                             ipip_mode="cross-subnet")
            self.modify_subnet(host1, 32)
            self.modify_subnet(host2, 32)
            self.assert_ipip_routing(host1, workload_host1, workload_host2,
                                     True)

    def test_ipip_addr_assigned(self):
        with DockerHost('host', dind=False, start_calico=False) as host:
            # Set up first pool before Node is started, to ensure we get tunl IP on boot
            ipv4_pool = IPNetwork("10.0.1.0/24")
            self.pool_action(host, "create", ipv4_pool, True)
            host.start_calico_node()
            self.assert_tunl_ip(host, ipv4_pool, expect=True)

            # Test that removing tunl removes the tunl IP.
            self.pool_action(host, "delete", ipv4_pool, True)
            self.assert_tunl_ip(host, ipv4_pool, expect=False)

            # Test that re-adding the pool triggers the confd watch and we get an IP
            self.pool_action(host, "create", ipv4_pool, True)
            self.assert_tunl_ip(host, ipv4_pool, expect=True)

            # Test that by adding another pool, then deleting the first,
            # we remove the original IP, and allocate a new one from the new pool
            new_ipv4_pool = IPNetwork("192.168.0.0/16")
            self.pool_action(host, "create", new_ipv4_pool, True)
            self.pool_action(host, "delete", ipv4_pool, True)
            self.assert_tunl_ip(host, new_ipv4_pool)

    def pool_action(self, host, action, cidr, ipip, ipip_mode=""):
        """
        Perform an ipPool action.
        """
        testdata = {
            'apiVersion': 'v1',
            'kind': 'ipPool',
            'metadata': {
                'cidr': str(cidr)
            },
            'spec': {
                'ipip': {
                    'enabled': ipip
                }
            }
        }
        if ipip_mode:
            testdata['spec']['ipip']['mode'] = ipip_mode
        host.writefile("testfile.yaml", testdata)
        host.calicoctl("%s -f testfile.yaml" % action)

    def assert_tunl_ip(self, host, ip_network, expect=True):
        """
        Helper function to make assertions on whether or not the tunl interface
        on the Host has been assigned an IP or not. This function will retry
        7 times, ensuring that our 5 second confd watch will trigger.

        :param host: DockerHost object
        :param ip_network: IPNetwork object which describes the ip-range we do (or do not)
        expect to see an IP from on the tunl interface.
        :param expect: Whether or not we are expecting to see an IP from IPNetwork on the tunl interface.
        :return:
        """
        retries = 7
        for retry in range(retries + 1):
            try:
                output = host.execute("ip addr show tunl0")
                match = re.search(r'inet ([\d]{1,3}\.[\d]{1,3}\.[\d]{1,3}\.[\d]{1,3})', output)
                if match:
                    ip_address = IPAddress(match.group(1))
                    if expect:
                        self.assertIn(ip_address, ip_network)
                    else:
                        self.assertNotIn(ip_address, ip_network)
                else:
                    self.assertFalse(expect, "No IP address assigned to tunl interface.")
            except Exception as e:
                if retry < retries:
                    sleep(1)
                else:
                    raise e
            else:
                return

    def remove_tunl_ip(self):
        """
        Remove the host tunl IP address if assigned.
        """
        try:
            output = subprocess.check_output(["ip", "addr", "show", "tunl0"])
        except subprocess.CalledProcessError:
            return

        match = re.search(r'inet ([\d]{1,3}\.[\d]{1,3}\.[\d]{1,3}\.[\d]{1,3})', output)
        if not match:
            return

        ipnet = str(IPNetwork(match.group(1)))

        try:
            output = subprocess.check_output(["ip", "addr", "del", ipnet, "dev", "tunl0"])
        except subprocess.CalledProcessError:
            return

    def modify_subnet(self, host, prefixlen):
        """
        Update the calico node resource to use the specified prefix length.

        Returns the current mask size.
        """
        node = json.loads(host.calicoctl(
            "get node %s --output=json" % host.get_hostname()))
        assert len(node) == 1

        # Get the current network and prefix len
        ipnet = IPNetwork(node[0]["spec"]["bgp"]["ipv4Address"])
        current_prefix_len = ipnet.prefixlen

        # Update the prefix length
        ipnet.prefixlen = prefixlen
        node[0]["spec"]["bgp"]["ipv4Address"] = str(ipnet)

        # Write the data back again.
        host.writejson("new_data", node)
        host.calicoctl("apply -f new_data")
        return current_prefix_len

    def assert_ipip_routing(self, host1, workload_host1, workload_host2, expect_ipip):
        """
        Test whether IPIP is being used as expected on host1 when pinging workload_host2
        from workload_host1.
        """
        def check():
            orig_tx = self.get_tunl_tx(host1)
            workload_host1.execute("ping -c 2 -W 1 %s" % workload_host2.ip)
            if expect_ipip:
                assert self.get_tunl_tx(host1) == orig_tx + 2
            else:
                assert self.get_tunl_tx(host1) == orig_tx
        retry_until_success(check)

    def get_tunl_tx(self, host):
        """
        Get the tunl TX count
        """
        try:
            output = host.execute("ifconfig tunl0")
        except subprocess.CalledProcessError:
            return

        match = re.search(r'RX packets:(\d+) ',
                          output)
        return int(match.group(1))
