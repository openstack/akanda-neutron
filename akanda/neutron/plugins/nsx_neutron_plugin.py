# Copyright 2014 DreamHost, LLC
#
# Author: DreamHost, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import functools
import os

from sqlalchemy import exc as sql_exc

from neutron.api import extensions as neutron_extensions
from neutron.common import constants
from neutron.common import exceptions as n_exc
from neutron.common import topics
from neutron.db import l3_db
from neutron.db import l3_rpc_base as l3_rpc
from neutron.extensions import portbindings as pbin
from neutron.openstack.common import log as logging
from neutron.openstack.common import rpc
from neutron.openstack.common.db import exception as db_exc
from neutron.plugins import vmware
from neutron.plugins.vmware.api_client import exception as api_exc
from neutron.plugins.vmware.common import config
from neutron.plugins.vmware.common import nsx_utils
from neutron.plugins.vmware.common import sync as nsx_sync
from neutron.plugins.vmware.dhcp_meta import rpc as nsx_rpc
from neutron.plugins.vmware.dbexts import db as nsx_db
from neutron.plugins.vmware.nsxlib import switch as switchlib
from neutron.plugins.vmware.plugins import base
from oslo.config import cfg

from akanda.neutron.plugins import decorators as akanda
from akanda.neutron.plugins import floatingip

LOG = logging.getLogger("NeutronPlugin")
akanda.monkey_patch_ipv6_generator()

NVP_EXT_PATH = os.path.join(os.path.dirname(__file__), 'extensions')


def akanda_nvp_ipv6_port_security_wrapper(f):
    @functools.wraps(f)
    def wrapper(lport_obj, mac_address, fixed_ips, port_security_enabled,
                security_profiles, queue_id, mac_learning_enabled,
                allowed_address_pairs):

        f(lport_obj, mac_address, fixed_ips, port_security_enabled,
          security_profiles, queue_id, mac_learning_enabled,
          allowed_address_pairs)

        # evaulate the state so that we only override the value when enabled
        # otherwise we are preserving the underlying behavior of the NVP plugin
        if port_security_enabled:
            # hotfix to enable egress mulitcast
            lport_obj['allow_egress_multicast'] = True

            # TODO(mark): investigate moving away from this an wrapping
            # (create|update)_port
            # add link-local and subnet cidr for IPv6 temp addresses
            special_ipv6_addrs = akanda.get_special_ipv6_addrs(
                (p['ip_address'] for p in lport_obj['allowed_address_pairs']),
                mac_address
            )

            lport_obj['allowed_address_pairs'].extend(
                {'mac_address': mac_address, 'ip_address': addr}
                for addr in special_ipv6_addrs
            )

    return wrapper


base.switchlib._configure_extensions = akanda_nvp_ipv6_port_security_wrapper(
    base.switchlib._configure_extensions
)


class AkandaNsxRpcCallbacks(l3_rpc.L3RpcCallbackMixin,
                            nsx_rpc.NSXRpcCallbacks):
    """ Class TODO:(rods)"""
    pass


class NsxSynchronizer(nsx_sync.NsxSynchronizer):
    """
    The NsxSynchronizer class in Neutron runs a synchronization thread to
    sync nvp objects with neutron objects. Since we don't use nvp's routers
    the sync was failing making neutron showing all the routers like if the
    were in Error state. To fix this behaviour we override the two methods
    responsible for the routers synchronization in the NsxSynchronizer class
    to be a noop

    """
    def _synchronize_lrouters(self, *args, **kwargs):
        pass

    def synchronize_router(self, *args, **kwargs):
        pass


class NsxPluginV2(floatingip.ExplicitFloatingIPAllocationMixin,
                  base.NsxPluginV2):
    """
    NsxPluginV2 is a Neutron plugin that provides L2 Virtual Network
    functionality using NSX.
    """
    supported_extension_aliases = (
        base.NsxPluginV2.supported_extension_aliases +
        akanda.SUPPORTED_EXTENSIONS
    )

    def __init__(self):
        super(NsxPluginV2, self).__init__()
        config.validate_config_options()
        # TODO(salv-orlando): Replace These dicts with
        # collections.defaultdict for better handling of default values
        # Routines for managing logical ports in NSX
        self.port_special_owners = [l3_db.DEVICE_OWNER_ROUTER_GW,
                                    l3_db.DEVICE_OWNER_ROUTER_INTF]

        # ---------------------------------------------------------------------
        # Original code:
        # self._port_drivers = {
        #     'create': {l3_db.DEVICE_OWNER_ROUTER_GW:
        #                self._nsx_create_ext_gw_port,
        #                l3_db.DEVICE_OWNER_FLOATINGIP:
        #                self._nsx_create_fip_port,
        #                l3_db.DEVICE_OWNER_ROUTER_INTF:
        #                self._nsx_create_router_port,
        #                networkgw_db.DEVICE_OWNER_NET_GW_INTF:
        #                self._nsx_create_l2_gw_port,
        #                'default': self._nsx_create_port},
        #     'delete': {l3_db.DEVICE_OWNER_ROUTER_GW:
        #                self._nsx_delete_ext_gw_port,
        #                l3_db.DEVICE_OWNER_ROUTER_INTF:
        #                self._nsx_delete_router_port,
        #                l3_db.DEVICE_OWNER_FLOATINGIP:
        #                self._nsx_delete_fip_port,
        #                networkgw_db.DEVICE_OWNER_NET_GW_INTF:
        #                self._nsx_delete_port,
        #                'default': self._nsx_delete_port}
        # }

        self._port_drivers = {
            'create': {
                l3_db.DEVICE_OWNER_FLOATINGIP: self._nsx_create_fip_port,
                'default': self._nsx_create_port
            },
            'delete': {
                l3_db.DEVICE_OWNER_FLOATINGIP: self._nsx_delete_fip_port,
                'default': self._nsx_delete_port
            }
        }
        # ---------------------------------------------------------------------

        neutron_extensions.append_api_extensions_path([vmware.NSX_EXT_PATH])
        self.nsx_opts = cfg.CONF.NSX
        self.nsx_sync_opts = cfg.CONF.NSX_SYNC
        self.cluster = nsx_utils.create_nsx_cluster(
            cfg.CONF,
            self.nsx_opts.concurrent_connections,
            self.nsx_opts.nsx_gen_timeout)

        self.base_binding_dict = {
            pbin.VIF_TYPE: pbin.VIF_TYPE_OVS,
            pbin.VIF_DETAILS: {
                # TODO(rkukura): Replace with new VIF security details
                pbin.CAP_PORT_FILTER:
                'security-group' in self.supported_extension_aliases}}

        self._extend_fault_map()
        self.setup_dhcpmeta_access()
        # Set this flag to false as the default gateway has not
        # been yet updated from the config file
        self._is_default_net_gw_in_sync = False
        # Create a synchronizer instance for backend sync
        # ---------------------------------------------------------------------
        # Note(rods):
        # We added this code with the only purpose to make the nsx driver use
        # our subclass of the NsxSynchronizer object.
        #
        # DHC-2385
        #
        # Original code:
        # self._synchronizer = sync.NsxSynchronizer(
        #     self, self.cluster,
        #     self.nsx_sync_opts.state_sync_interval,
        #     self.nsx_sync_opts.min_sync_req_delay,
        #     self.nsx_sync_opts.min_chunk_size,
        #     self.nsx_sync_opts.max_random_sync_delay)

        self._synchronizer = nsx_sync.NsxSynchronizer(
            self, self.cluster,
            self.nsx_sync_opts.state_sync_interval,
            self.nsx_sync_opts.min_sync_req_delay,
            self.nsx_sync_opts.min_chunk_size,
            self.nsx_sync_opts.max_random_sync_delay)
        # ---------------------------------------------------------------------

    def setup_dhcpmeta_access(self):
        # Ok, so we're going to add L3 here too with the DHCP
        self.conn = rpc.create_connection(new=True)
        self.conn.create_consumer(
            topics.PLUGIN,
            AkandaNsxRpcCallbacks().create_rpc_dispatcher(),
            fanout=False
        )

        # Consume from all consumers in a thread
        self.conn.consume_in_thread()

        self.handle_network_dhcp_access_delegate = noop
        self.handle_port_dhcp_access_delegate = noop
        self.handle_port_metadata_access_delegate = noop
        self.handle_metadata_access_delegate = noop

    @akanda.auto_add_other_resources
    @akanda.auto_add_ipv6_subnet
    def create_network(self, context, network):
        return super(NsxPluginV2, self).create_network(context, network)

    @akanda.auto_add_subnet_to_router
    def create_subnet(self, context, subnet):
        return super(NsxPluginV2, self).create_subnet(context, subnet)

    # we need to use original versions l3_db.L3_NAT_db_mixin mixin and not
    # NSX versions that manage NSX's logical router

    create_router = l3_db.L3_NAT_db_mixin.create_router
    update_router = l3_db.L3_NAT_db_mixin.update_router
    delete_router = l3_db.L3_NAT_db_mixin.delete_router
    get_router = l3_db.L3_NAT_db_mixin.get_router
    get_routers = l3_db.L3_NAT_db_mixin.get_routers
    add_router_interface = l3_db.L3_NAT_db_mixin.add_router_interface
    remove_router_interface = l3_db.L3_NAT_db_mixin.remove_router_interface
    update_floatingip = l3_db.L3_NAT_db_mixin.update_floatingip
    delete_floatingip = l3_db.L3_NAT_db_mixin.delete_floatingip
    get_floatingip = l3_db.L3_NAT_db_mixin.get_floatingip
    get_floatings = l3_db.L3_NAT_db_mixin.get_floatingips
    _update_fip_assoc = l3_db.L3_NAT_db_mixin._update_fip_assoc
    _update_router_gw_info = l3_db.L3_NAT_db_mixin._update_router_gw_info
    disassociate_floatingips = l3_db.L3_NAT_db_mixin.disassociate_floatingips

    def _ensure_metadata_host_route(self, *args, **kwargs):
        """ Akanda metadata services are provided by router so make no-op/"""
        pass

    def _nsx_create_port(self, context, port_data):
        """Driver for creating a logical switch port on NSX platform."""
        # FIXME(salvatore-orlando): On the NSX platform we do not really have
        # external networks. So if as user tries and create a "regular" VIF
        # port on an external network we are unable to actually create.
        # However, in order to not break unit tests, we need to still create
        # the DB object and return success

        # NOTE(rods): Reporting mark's comment on havana version of this patch.
        # Akanda does want ports for external networks so this method is
        # basically same with external check removed and the auto plugging of
        # router ports

        # ---------------------------------------------------------------------
        # Note(rods): Remove the check on the external network
        #
        # Original code:
        # if self._network_is_external(context, port_data['network_id']):
        #     LOG.info(_("NSX plugin does not support regular VIF ports on "
        #                "external networks. Port %s will be down."),
        #              port_data['network_id'])
        #     # No need to actually update the DB state - the default is down
        #     return port_data
        # ---------------------------------------------------------------------
        lport = None
        selected_lswitch = None
        try:
            selected_lswitch = self._nsx_find_lswitch_for_port(context,
                                                               port_data)
            lport = self._nsx_create_port_helper(context.session,
                                                 selected_lswitch['uuid'],
                                                 port_data,
                                                 True)
            nsx_db.add_neutron_nsx_port_mapping(
                context.session, port_data['id'],
                selected_lswitch['uuid'], lport['uuid'])
            # -----------------------------------------------------------------
            # Note(rods): Auto plug router ports
            #
            # Original code:
            # if port_data['device_owner'] not in self.port_special_owners:
            #     switchlib.plug_vif_interface(
            #         self.cluster, selected_lswitch['uuid'],
            #         lport['uuid'], "VifAttachment", port_data['id'])

            switchlib.plug_vif_interface(
                self.cluster, selected_lswitch['uuid'],
                lport['uuid'], "VifAttachment", port_data['id'])
            # -----------------------------------------------------------------

            LOG.debug(_("_nsx_create_port completed for port %(name)s "
                        "on network %(network_id)s. The new port id is "
                        "%(id)s."), port_data)
        except (api_exc.NsxApiException, n_exc.NeutronException):
            self._handle_create_port_exception(
                context, port_data['id'],
                selected_lswitch and selected_lswitch['uuid'],
                lport and lport['uuid'])
        except db_exc.DBError as e:
            if (port_data['device_owner'] == constants.DEVICE_OWNER_DHCP and
                    isinstance(e.inner_exception, sql_exc.IntegrityError)):
                msg = (_("Concurrent network deletion detected; Back-end Port "
                         "%(nsx_id)s creation to be rolled back for Neutron "
                         "port: %(neutron_id)s")
                       % {'nsx_id': lport['uuid'],
                          'neutron_id': port_data['id']})
                LOG.warning(msg)
                if selected_lswitch and lport:
                    try:
                        switchlib.delete_port(self.cluster,
                                              selected_lswitch['uuid'],
                                              lport['uuid'])
                    except n_exc.NotFound:
                        LOG.debug(_("NSX Port %s already gone"), lport['uuid'])

    def _nsx_delete_port(self, context, port_data):
        # FIXME(salvatore-orlando): On the NSX platform we do not really have
        # external networks. So deleting regular ports from external networks
        # does not make sense. However we cannot raise as this would break
        # unit tests.

        # NOTE(rods): reporting mark's comment on havana version of this patch.
        # Akanda does want ports for external networks so this method is
        # basically same with external check removed

        # ---------------------------------------------------------------------
        # Original code:
        # if self._network_is_external(context, port_data['network_id']):
        #     LOG.info(_("NSX plugin does not support regular VIF ports on "
        #                "external networks. Port %s will be down."),
        #              port_data['network_id'])
        #     return
        # ---------------------------------------------------------------------

        nsx_switch_id, nsx_port_id = nsx_utils.get_nsx_switch_and_port_id(
            context.session, self.cluster, port_data['id'])
        if not nsx_port_id:
            LOG.debug(_("Port '%s' was already deleted on NSX platform"), id)
            return
        # TODO(bgh): if this is a bridged network and the lswitch we just got
        # back will have zero ports after the delete we should garbage collect
        # the lswitch.
        try:
            switchlib.delete_port(self.cluster, nsx_switch_id, nsx_port_id)
            LOG.debug(_("_nsx_delete_port completed for port %(port_id)s "
                        "on network %(net_id)s"),
                      {'port_id': port_data['id'],
                       'net_id': port_data['network_id']})
        except n_exc.NotFound:
            LOG.warning(_("Port %s not found in NSX"), port_data['id'])


def noop(*args, **kwargs):
    pass
