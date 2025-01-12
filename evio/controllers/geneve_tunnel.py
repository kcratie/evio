# EdgeVPNio
# Copyright 2020, University of Florida
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import time

from broker import GENEVE_SETUP_TIMEOUT
from broker.controller_module import ControllerModule
from broker.remote_action import RemoteAction
from pyroute2 import IPRoute

from .tunnel import DATAPLANE_TYPES, TUNNEL_EVENTS, TUNNEL_STATES, Tunnel


class GeneveTunnel(ControllerModule):
    TAPNAME_MAXLEN = 15
    _REFLECT: list[str] = ["_tunnels"]

    def __init__(self, nexus, module_config):
        super().__init__(nexus, module_config)
        self._tunnels: dict[str, Tunnel] = {}  # tunnel id -> TunnelDescriptor
        self._gnv_updates_publisher = None

    def initialize(self):
        self._register_abort_handlers()
        self._register_req_handlers()
        self._register_resp_handlers()
        self._gnv_updates_publisher = self.publish_subscription("GNV_TUNNEL_EVENTS")
        self.logger.info("Controller module loaded")

    def _register_abort_handlers(self):
        self._abort_handler_tbl = {
            "SIG_REMOTE_ACTION": self.abort_handler_remote_action
        }

    def _register_req_handlers(self):
        self._req_handler_tbl = {
            "GNV_AUTH_TUNNEL": self.req_handler_auth_tunnel,
            "GNV_CREATE_TUNNEL": self.req_handler_create_tunnel,
            "GNV_REMOVE_TUNNEL": self.req_handler_remove_tunnel,
            "GNV_EXCHANGE_ENDPT": self.req_handler_exchnge_endpt,
            "GNV_UPDATE_MAC": self.req_handler_update_peer_mac,
            "GNV_CANCEL_TUNNEL": self.req_handler_abort_tunnel,
        }

    def _register_resp_handlers(self):
        self._resp_handler_tbl = {"SIG_REMOTE_ACTION": self.resp_handler_remote_action}

    def terminate(self):
        for tnl in self._tunnels.values():
            self._remove_tunnel(tnl.tap_name)
        self._tunnels.clear()
        self.logger.info("Controller module terminating")

    def _deauth_tnls(self, tnls: list):
        for tnl in tnls:
            self.logger.info("Deauthorizing expired tunnel %s", tnl)
            self._tunnels.pop(tnl.tnlid, None)

    def _rollback_tnls(self, tnls: list):
        for tnl in tnls:
            self.logger.info("Removing expired tunnel %s", tnl)
            self._tunnels.pop(tnl.tnlid, None)
            self._remove_tunnel(tnl.tap_name)
            param = {
                "UpdateType": TUNNEL_EVENTS.Removed,
                "OverlayId": tnl.overlay_id,
                "PeerId": tnl.peer_id,
                "TunnelId": tnl.tnlid,
                "TapName": tnl.tap_name,
            }
            self._gnv_updates_publisher.post_update(param)

    def _create_tunnel(self, tap_name, vnid, remote_addr):
        self.logger.info(
            "Creating Geneve tunnel %s vnid=%s, remote addr=%s",
            tap_name,
            vnid,
            remote_addr,
        )
        with IPRoute() as ipr:
            ipr.link(
                "add",
                ifname=tap_name,
                kind="geneve",
                geneve_id=vnid,
                geneve_remote=remote_addr,
            )
            idx = ipr.link_lookup(ifname=tap_name)[0]
            ipr.link("set", index=idx, state="up")

    def _remove_tunnel(self, tap_name):
        try:
            self.logger.info("Removing Geneve tunnel %s", tap_name)
            with IPRoute() as ipr:
                idx = ipr.link_lookup(ifname=tap_name)
                if len(idx) > 0:
                    idx = idx[0]
                    ipr.link("set", index=idx, state="down")
                    ipr.link("del", index=idx)
        except Exception as e:
            self.logger.warning(
                "Failed to remove geneve tunnel %s, error code: %s", tap_name, e
            )

    def _is_tap_exist(self, tap_name):
        with IPRoute() as ipr:
            idx = ipr.link_lookup(ifname=tap_name)
            if len(idx) == 1:
                return True
        return False

    def _is_tunnel_authorized(self, tunnel_id):
        tnl = self._tunnels.get(tunnel_id)
        if tnl and tnl.state == TUNNEL_STATES.AUTHORIZED:
            return True
        return False

    def req_handler_auth_tunnel(self, cbt):
        """Node B"""
        olid = cbt.request.params["OverlayId"]
        peer_id = cbt.request.params["PeerId"]
        tnlid = cbt.request.params["TunnelId"]
        if tnlid in self._tunnels:
            cbt.set_response(
                f"Geneve tunnel authorization failed, a tunnel with ID {tnlid[:7]}"
                f"already exist for peer{peer_id[:7]}",
                False,
            )
        else:
            tap_name = self.get_tap_name(peer_id, olid)
            tnl = Tunnel(
                tnlid,
                olid,
                peer_id,
                TUNNEL_STATES.AUTHORIZED,
                tap_name,
                DATAPLANE_TYPES.Geneve,
            )
            self._tunnels[tnlid] = tnl
            self.register_timed_transaction(
                tnl,
                self.is_tnl_completed,
                self.on_tnl_timeout,
                GENEVE_SETUP_TIMEOUT,
            )
            self.logger.debug(
                "TunnelId:%s authorization for Peer:%s completed",
                tnlid[:7],
                peer_id[:7],
            )
            cbt.set_response(
                f"Geneve tunnel authorization completed, TunnelId:{tnlid[:7]}", True
            )
            event_param = {
                "UpdateType": TUNNEL_EVENTS.Authorized,
                "OverlayId": olid,
                "PeerId": peer_id,
                "TunnelId": tnlid,
            }
            self._gnv_updates_publisher.post_update(event_param)
        self.complete_cbt(cbt)

    def req_handler_create_tunnel(self, cbt):
        """Role A. Issued from local Topology."""
        olid = cbt.request.params["OverlayId"]
        tnlid = cbt.request.params["TunnelId"]
        loc_id = cbt.request.params["VNId"]
        peer_id = cbt.request.params["PeerId"]
        tap_name = self.get_tap_name(peer_id, olid)

        if tnlid in self._tunnels:
            cbt.set_response(data=f"Tunnel {tnlid} already exists", status=False)
            self.complete_cbt(cbt)
        if self._is_tap_exist(tap_name):
            # delete remenants
            self._remove_tunnel(tap_name)

        self._tunnels[tnlid] = Tunnel(
            tnlid,
            olid,
            peer_id,
            TUNNEL_STATES.CREATING,
            tap_name,
            DATAPLANE_TYPES.Geneve,
        )
        params = {
            "OverlayId": olid,
            "NodeId": self.node_id,
            "TunnelId": tnlid,
            "VNId": loc_id,
            "EndPointAddress": self.config["Overlays"][olid]["EndPointAddress"],
        }
        rem_act = RemoteAction(
            overlay_id=olid,
            recipient_id=peer_id,
            recipient_cm="GeneveTunnel",
            action="GNV_EXCHANGE_ENDPT",
            params=params,
        )
        rem_act.submit_remote_act(self, cbt)

    def req_handler_exchnge_endpt(self, cbt):
        """Role B
        Node B accepts data from node A, creates tunnel on node B and completes cbt
        """
        try:
            params = cbt.request.params
            olid = params["OverlayId"]
            tnlid = params["TunnelId"]
            vnid = params["VNId"]
            peer_id = params["NodeId"]
            endpnt_address = params["EndPointAddress"]
            tap_name = self._tunnels[tnlid].tap_name
            if not self._is_tunnel_authorized(tnlid):
                emsg = str(
                    "The requested link endpoint was not authorized or has expired. It will not be created. "
                    f"TunnelId={tnlid}, PeerId={peer_id}, VNID={vnid}"
                )
                raise RuntimeWarning(emsg)

            if self._is_tap_exist(tap_name):
                self._remove_tunnel(tap_name)
            self._create_tunnel(tap_name, vnid, endpnt_address)
            self._tunnels[tnlid].state = TUNNEL_STATES.CREATING
            resp = {
                "EndPointAddress": self.config["Overlays"][olid]["EndPointAddress"],
                "VNId": vnid,
                "NodeId": self.node_id,
                "TunnelId": tnlid,
                "MAC": self._tunnels[tnlid].mac,
                "Dataplane": self._tunnels[tnlid].dataplane,
            }
            cbt.set_response(resp, True)
            self.complete_cbt(cbt)
        except Exception as err:
            self.logger.warning("Failed to create Geneve tunnel %s. %s", tnlid, err)
            msg = f"Node {self.node_id} failed to create Geneve tunnel {tnlid}. {err}"
            self._tunnels[tnlid].state = TUNNEL_STATES.OFFLINE
            self._tunnels.pop(tnlid, None)
            cbt.set_response(msg, False)
            self.complete_cbt(cbt)

    def req_handler_update_peer_mac(self, cbt):
        """Role B"""
        params = cbt.request.params
        olid = params["OverlayId"]
        tnlid = params["TunnelId"]
        peer_id = params["NodeId"]
        if tnlid not in self._tunnels:
            cbt.set_response(f"Tunnel {tnlid} does not  exist", False)
        elif self._tunnels[tnlid].state == TUNNEL_STATES.OFFLINE:
            # the in-progress tunnel was removed in the deauth method
            cbt.set_response("Tunnel {tnlid} failed and was destroyed", False)
        elif self._tunnels[tnlid].state == TUNNEL_STATES.CREATING:
            self._tunnels[tnlid].peer_mac = params["MAC"]
            # tunnel connected is simulated here, BF module will check peer liveliness
            self._tunnels[tnlid].state = TUNNEL_STATES.ONLINE
            gnv_param = {
                "UpdateType": TUNNEL_EVENTS.Connected,
                "OverlayId": olid,
                "PeerId": peer_id,
                "TunnelId": tnlid,
                "ConnectedTimestamp": time.time(),
                "TapName": self._tunnels[tnlid].tap_name,
                "MAC": self._tunnels[tnlid].mac,
                "PeerMac": self._tunnels[tnlid].peer_mac,
                "Dataplane": self._tunnels[tnlid].dataplane,
            }
            self._gnv_updates_publisher.post_update(gnv_param)
            cbt.set_response("Peer MAC added", True)
        else:
            cbt.set_response(f"Invalid request for Tunnel {tnlid}", False)
        self.complete_cbt(cbt)

    def req_handler_abort_tunnel(self, cbt):
        """
        Role B
        Operation should always succeed.
        """
        olid = cbt.request.params["OverlayId"]
        peer_id = cbt.request.params["PeerId"]
        tnlid = cbt.request.params["TunnelId"]
        self.logger("Removing aborted Geneve tunnel %s to %s", peer_id, tnlid)
        if tnlid in self._tunnels:
            tnl = self._tunnels[tnlid]
            tap_name = tnl.tap_name
            if self._is_tap_exist(tap_name):
                if tnl.state in (TUNNEL_STATES.AUTHORIZED, TUNNEL_STATES.CREATING):
                    self._deauth_tnls(
                        [tnlid]
                    )  # only send the auth expired event from these states
                tnl.state = TUNNEL_STATES.OFFLINE
        else:
            # even if there is no tunnel in the map remove the TAP if it exists
            tap_name = self.get_tap_name(olid, peer_id)
            self._remove_tunnel(tap_name)
        cbt.set_response(data=f"Tunnel aborted: {tnlid}", status=True)
        self.complete_cbt(cbt)

    def req_handler_remove_tunnel(self, cbt):
        """
        Issued from local Topology. Operation always succeed.
        """
        peer_id = cbt.request.params["PeerId"]
        olid = cbt.request.params["OverlayId"]
        tnlid = cbt.request.params["TunnelId"]
        if tnlid in self._tunnels:
            tnl = self._tunnels[tnlid]
            tap_name = tnl.tap_name
            if tnl.state != TUNNEL_STATES.ONLINE:
                self.logger.warning(
                    f"In request to remove Geneve Tunnel, it's state is not ONLINE: {str(tnl)}"
                )

            self._tunnels[tnlid].state = TUNNEL_STATES.OFFLINE
            self._remove_tunnel(tap_name)
            self._tunnels.pop(tnlid)
            cbt.set_response(data=f"Tunnel deleted {tnlid}", status=True)
        else:
            # even if there is no tunnel in the map remove the TAP if it exists
            tap_name = self.get_tap_name(olid, peer_id)
            self._remove_tunnel(tap_name)
            cbt.set_response(data=f"Tunnel deleted {tap_name}", status=True)
        self.complete_cbt(cbt)
        gnv_param = {
            "UpdateType": TUNNEL_EVENTS.Removed,
            "OverlayId": olid,
            "PeerId": peer_id,
            "TunnelId": tnlid,
            "TapName": tap_name,
        }
        self._gnv_updates_publisher.post_update(gnv_param)

    def resp_handler_remote_action(self, cbt):
        """Role A"""
        parent_cbt = cbt.parent
        rem_act = cbt.request.params
        if not cbt.response.status:
            olid = rem_act.overlay_id
            peer_id = rem_act.recipient_id
            tnlid = rem_act.params["TunnelId"]
            if tnlid in self._tunnels:
                self._tunnels[tnlid].state = TUNNEL_STATES.OFFLINE
                self._tunnels.pop(tnlid)
            tap_name = self.get_tap_name(olid, peer_id)
            self._remove_tunnel(tap_name)
            self.free_cbt(cbt)
            if parent_cbt:
                parent_cbt.set_response(cbt.response.data, False)
                self.complete_cbt(parent_cbt)
        elif rem_act.action == "GNV_EXCHANGE_ENDPT":
            try:
                rem_act = cbt.response.data
                olid = rem_act.overlay_id
                peer_id = rem_act.recipient_id
                tnlid = rem_act.data["TunnelId"]
                vnid = rem_act.data["VNId"]
                self._tunnels[tnlid].peer_mac = rem_act.data["MAC"]
                endpnt_address = rem_act.data["EndPointAddress"]
                tap_name = self.get_tap_name(peer_id, olid)
                self._create_tunnel(tap_name, vnid, endpnt_address)
                act_code = "GNV_UPDATE_MAC"
                self.free_cbt(cbt)
            except Exception as err:
                # local failure so send abort to remote
                msg = f"Failed to create Geneve tunnel {tnlid}. Error={err}"
                self.logger.warning("Geneve endpoint exchage failed %s. %s", tnlid, err)
                self.free_cbt(cbt)
                parent_cbt.set_response(msg, False)
                self.complete_cbt(parent_cbt)
                parent_cbt = None
                act_code = "GNV_CANCEL_TUNNEL"
            finally:
                params = {
                    "OverlayId": olid,
                    "NodeId": self.node_id,
                    "TunnelId": tnlid,
                    "MAC": self._tunnels[tnlid].mac,
                }
                remote_act = RemoteAction(
                    overlay_id=olid,
                    recipient_id=peer_id,
                    recipient_cm="GeneveTunnel",
                    action=act_code,
                    params=params,
                )
                remote_act.submit_remote_act(self, parent_cbt)
        elif rem_act.action == "GNV_UPDATE_MAC":
            rem_act = cbt.response.data
            olid = rem_act.overlay_id
            peer_id = rem_act.recipient_id
            tnlid = rem_act.params["TunnelId"]
            self._tunnels[tnlid].state = TUNNEL_STATES.ONLINE
            gnv_param = {
                "UpdateType": TUNNEL_EVENTS.Connected,
                "OverlayId": olid,
                "PeerId": peer_id,
                "TunnelId": tnlid,
                "ConnectedTimestamp": time.time(),
                "TapName": self._tunnels[tnlid].tap_name,
                "MAC": self._tunnels[tnlid].mac,
                "PeerMac": self._tunnels[tnlid].peer_mac,
                "Dataplane": self._tunnels[tnlid].dataplane,
            }
            self._gnv_updates_publisher.post_update(gnv_param)
            self.free_cbt(cbt)
            parent_cbt.set_response("Geneve tunnel created", True)
            self.complete_cbt(parent_cbt)
        elif rem_act.action == "GNV_CANCEL_TUNNEL":
            self.free_cbt(cbt)

    def abort_handler_remote_action(self, cbt):
        parent_cbt = cbt.parent
        rem_act = cbt.request.params
        if rem_act.action == "GNV_EXCHANGE_ENDPT":
            olid = rem_act.overlay_id
            peer_id = rem_act.recipient_id
            tnlid = rem_act.params["TunnelId"]
            if tnlid in self._tunnels:
                self._tunnels[tnlid].state = TUNNEL_STATES.OFFLINE
                self._tunnels.pop(tnlid)
            tap_name = self.get_tap_name(olid, peer_id)
            self._remove_tunnel(tap_name)
            self.free_cbt(cbt)
            if parent_cbt:
                parent_cbt.set_response(cbt.response.data, False)
                self.complete_cbt(parent_cbt)

    def get_tap_name(self, peer_id, olid) -> str:
        tap_name_prefix = self.config["Overlays"][olid].get("TapNamePrefix", olid[:5])
        end_i = self.TAPNAME_MAXLEN - len(tap_name_prefix)
        tap_name = tap_name_prefix + str(peer_id[:end_i])
        return tap_name

    def is_tnl_completed(self, tnl: Tunnel) -> bool:
        return bool(tnl.state == TUNNEL_STATES.ONLINE)

    def on_tnl_timeout(self, tnl: Tunnel, timeout: float):
        deauth = []
        rlbk = []
        for tnl in self._tunnels.values():
            if tnl.state == TUNNEL_STATES.AUTHORIZED:
                deauth.append(tnl)
            elif tnl.state in (TUNNEL_STATES.CREATING, TUNNEL_STATES.TNL_OFFLINE):
                rlbk.append(tnl)
        self._deauth_tnls(deauth)
        self._rollback_tnls(rlbk)
