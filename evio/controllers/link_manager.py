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

import threading
import time

import broker
from broker import LINK_SETUP_TIMEOUT
from broker.cbt import CBT
from broker.controller_module import ControllerModule
from broker.remote_action import RemoteAction

from .tunnel import DATAPLANE_TYPES, TUNNEL_EVENTS, TUNNEL_STATES


class Link:
    _REFLECT: list[str] = ["lnkid", "_creation_state", "status_retry"]

    def __init__(self, lnkid, state):
        self.lnkid = lnkid
        self._creation_state = state
        self.status_retry = 0
        self.stats = {}

    def __repr__(self):
        return broker.introspect(self)

    @property
    def creation_state(self):
        return self._creation_state

    @creation_state.setter
    def creation_state(self, new_state):
        self._creation_state = new_state


class Tunnel:
    def __init__(
        self,
        tnlid: str,
        overlay_id: str,
        peer_id: str,
        tnl_state,
        dataplane,
        dp_instance_id: int,
    ):
        self.tnlid = tnlid
        self.overlay_id = overlay_id
        self.peer_id = peer_id
        self.tap_name = None
        self.mac = None
        self.fpr = None
        self.link = None
        self.peer_mac = None
        self._tunnel_state = tnl_state
        self.dataplane = dataplane
        self.dp_instance_id = dp_instance_id

    def __repr__(self):
        return broker.introspect(self)

    @property
    def tunnel_state(self):
        return self._tunnel_state

    @tunnel_state.setter
    def tunnel_state(self, new_state):
        self._tunnel_state = new_state


class LinkManager(ControllerModule):
    TAPNAME_MAXLEN = 15
    _REFLECT: list[str] = ["_tunnels"]

    def __init__(self, nexus, module_config):
        super().__init__(nexus, module_config)
        self._tunnels: dict[str, Tunnel] = {}  # maps tunnel id to its descriptor
        self._links = {}  # maps link id to tunnel id
        self._lock = threading.Lock()  # serializes access to _overlays, _links
        self._link_updates_publisher = None
        self._ignored_net_interfaces = dict()
        self._tc_session_id: int = 0

    def initialize(self):
        self._register_abort_handlers()
        self._register_req_handlers()
        self._register_resp_handlers()
        self._link_updates_publisher = self.publish_subscription("LNK_TUNNEL_EVENTS")
        publishers = self.get_registered_publishers()
        if (
            "TincanTunnel" not in publishers
            or "TCI_TINCAN_MSG_NOTIFY"
            not in self.get_available_subscriptions("TincanTunnel")
        ):
            raise RuntimeError(
                "The TincanTunnel MESSAGE NOTIFY subscription is not available."
                "Link Manager cannot continue."
            )
        self.start_subscription("TincanTunnel", "TCI_TINCAN_MSG_NOTIFY")
        if (
            "OverlayVisualizer" in publishers
            and "VIS_DATA_REQ" in self.get_available_subscriptions("OverlayVisualizer")
        ):
            self.start_subscription("OverlayVisualizer", "VIS_DATA_REQ")
        else:
            self.logger.info("Overlay visualizer capability unavailable")

        for olid in self.config["Overlays"]:
            self._ignored_net_interfaces[olid] = set()
            ol_cfg = self.config["Overlays"][olid]
            if "IgnoredNetInterfaces" in ol_cfg:
                for ign_inf in ol_cfg["IgnoredNetInterfaces"]:
                    self._ignored_net_interfaces[olid].add(ign_inf)

        self.logger.info("Controller module loaded")

    @property
    def tc_session_id(self):
        return self._tc_session_id

    @tc_session_id.setter
    def tc_session_id(self, val: int):
        self.logger.info("Updating Tincan session ID %s->%s", self.tc_session_id, val)
        self._tc_session_id = val

    def terminate(self):
        self.logger.info("Controller module terminating")

    def abort_handler_tunnel(self, cbt: CBT):
        if isinstance(cbt.request.params, RemoteAction):
            tnlid = cbt.request.params.params["TunnelId"]
        else:
            tnlid = cbt.request.params["TunnelId"]
        tnl = self._tunnels.get(tnlid)
        self._cleanup_failed_tunnel_data(tnl)

    def req_handler_auth_tunnel(self, cbt: CBT):
        """Node B"""
        olid = cbt.request.params["OverlayId"]
        peer_id = cbt.request.params["PeerId"]
        tnlid = cbt.request.params["TunnelId"]
        if tnlid in self._tunnels:
            cbt.set_response(
                "Tunnel auth failed, resource already exist for peer:tunnel {0}:{1}".format(
                    peer_id, tnlid[:7]
                ),
                False,
            )
            self.complete_cbt(cbt)
        else:
            tnl = Tunnel(
                tnlid,
                olid,
                peer_id,
                tnl_state=TUNNEL_STATES.AUTHORIZED,
                dataplane=DATAPLANE_TYPES.Tincan,
                dp_instance_id=self.tc_session_id,
            )
            self._tunnels[tnlid] = tnl
            self.register_timed_transaction(
                tnl,
                self.is_link_completed,
                self.on_tnl_timeout,
                LINK_SETUP_TIMEOUT,
            )
            self.logger.debug(
                "TunnelId:%s auth for peer:%s completed", tnlid[:7], peer_id[:7]
            )
            cbt.set_response(
                "Authorization completed, TunnelId:{0}".format(tnlid[:7]), True
            )
            lnkupd_param = {
                "UpdateType": TUNNEL_EVENTS.Authorized,
                "OverlayId": olid,
                "PeerId": peer_id,
                "TunnelId": tnlid,
            }
            self.complete_cbt(cbt)
            self._link_updates_publisher.post_update(lnkupd_param)

    def req_handler_create_tunnel(self, cbt: CBT):
        """Create Link: Phase 1 Node A
        Handle the request for capability LNK_CREATE_TUNNEL.
        The caller provides the overlay id and the peer id which the link
        connects. The link id is set here to match the tunnel id, and it is returned
        to the caller after the local endpoint creation is completed asynchronously.
        The link is not necessarily ready for read/write at this time. The link
        status can be queried to determine when it is writeable. The link id is
        communicated in the request and will be the same at both nodes.
        """
        olid = cbt.request.params["OverlayId"]
        peer_id = cbt.request.params["PeerId"]
        tnlid = cbt.request.params["TunnelId"]
        if tnlid in self._tunnels:
            # Tunnel already exists
            tnl = self._tunnels[tnlid]
            if not tnl.link:
                # we need to create the link
                lnkid = tnlid
                self.logger.debug(
                    "Create Link:%s Tunnel exists. "
                    "Skipping phase 1/5 Node A - Peer: %s",
                    lnkid[:7],
                    peer_id[:7],
                )

                self.logger.debug(
                    "Create Link:%s Phase 2/5 Node A - Peer: %s", lnkid[:7], peer_id[:7]
                )
                self._assign_link_to_tunnel(tnlid, lnkid, 0xA2)
                tnl.tunnel_state = TUNNEL_STATES.CREATING
                # create and send remote action to request endpoint from peer
                params = {
                    "OverlayId": olid,
                    "TunnelId": tnlid,
                    "LinkId": lnkid,
                    "NodeData": {"FPR": tnl.fpr, "MAC": tnl.mac, "UID": self.node_id},
                }
                rem_act = RemoteAction(
                    overlay_id=olid,
                    recipient_id=peer_id,
                    recipient_cm="LinkManager",
                    action="LNK_REQ_LINK_ENDPT",
                    params=params,
                )
                rem_act.submit_remote_act(self, cbt)
            else:
                # Link already exists, TM should clean up first
                cbt.set_response(
                    "Failed, duplicate link requested to "
                    "overlay id: {0} peer id: {1}".format(olid, peer_id),
                    False,
                )
                self.complete_cbt(cbt)
            return
        # No tunnel exists, going to create it.
        tnlid = cbt.request.params["TunnelId"]
        lnkid = tnlid
        self._tunnels[tnlid] = Tunnel(
            tnlid,
            olid,
            peer_id,
            tnl_state=TUNNEL_STATES.CREATING,
            dataplane=DATAPLANE_TYPES.Tincan,
            dp_instance_id=self.tc_session_id,
        )
        self._assign_link_to_tunnel(tnlid, lnkid, 0xA1)

        self.logger.debug(
            "Create Link:%s Phase 1/5 Node A - Peer: %s", lnkid[:7], peer_id[:7]
        )
        params = {
            "OverlayId": olid,
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "PeerId": peer_id,
        }
        self._create_tunnel(params, parent_cbt=cbt)

    def req_handler_req_link_endpt(self, lnk_endpt_cbt):
        """Create Link: Phase 3 Node B
        Rcvd peer req to create endpt, send to TCI
        """
        params = lnk_endpt_cbt.request.params
        olid = params["OverlayId"]
        tnlid = params["TunnelId"]
        node_data = params["NodeData"]
        peer_id = node_data["UID"]
        if tnlid not in self._tunnels:
            msg = str(
                "The requested lnk endpt was not authorized it will not be created. "
                "TunnelId={0}, PeerId={1}".format(tnlid, peer_id)
            )
            self.logger.warning(msg)
            lnk_endpt_cbt.set_response(msg, False)
            self.complete_cbt(lnk_endpt_cbt)
            return
        if self._tunnels[tnlid].link:
            msg = str(
                "A link already exist for this tunnel, it will not be created. "
                "TunnelId={0}, PeerId={1}".format(tnlid, peer_id)
            )
            self.logger.warning(msg)
            lnk_endpt_cbt.set_response(msg, False)
            self.complete_cbt(lnk_endpt_cbt)
            return
        lnkid = tnlid
        self._tunnels[tnlid].tunnel_state = TUNNEL_STATES.CREATING
        self._tunnels[tnlid].dp_instance_id = self.tc_session_id
        self._assign_link_to_tunnel(tnlid, lnkid, 0xB1)
        self.logger.debug("Create Link:%s Phase 1/4 Node B", lnkid[:7])
        # Send request to Tincan
        tap_name = self._gen_tap_name(olid, peer_id)
        self.logger.debug(
            "IgnoredNetInterfaces: %s", self._get_ignored_tap_names(olid, tap_name)
        )
        create_link_params = {
            "OverlayId": olid,
            # overlay params
            "TunnelId": tnlid,
            "NodeId": self.node_id,
            "StunServers": self.config.get("Stun", []),
            "TapName": tap_name,
            "IgnoredNetInterfaces": list(self._get_ignored_tap_names(olid, tap_name)),
            # link params
            "LinkId": lnkid,
            "NodeData": {
                "FPR": node_data["FPR"],
                "MAC": node_data["MAC"],
                "UID": node_data["UID"],
            },
            "TincanId": self.tc_session_id,
        }
        if self.config.get("Turn"):
            create_link_params["TurnServers"] = self.config["Turn"]
        self.register_cbt(
            "TincanTunnel", "TCI_CREATE_LINK", create_link_params, lnk_endpt_cbt
        )

    def req_handler_add_peer_cas(self, cbt: CBT):
        # Create Link: Phase 7 Node B
        params = cbt.request.params
        lnkid = params["LinkId"]
        tnlid = self.tunnel_id(lnkid)
        peer_id = params["NodeData"]["UID"]
        if tnlid not in self._tunnels:
            self.logger.info(
                "A response to an aborted add peer CAS operation was discarded: %s",
                str(cbt),
            )
            cbt.set_response("This request was aborted", False)
            self.complete_cbt(cbt)
            return
        self._tunnels[tnlid].link.creation_state = 0xB3
        self.logger.debug(
            "Create Link:%s Phase 3/4 Node B - Peer: %s", lnkid[:7], peer_id[:7]
        )
        params["TincanId"] = self._tunnels[tnlid].dp_instance_id
        self.register_cbt("TincanTunnel", "TCI_CREATE_LINK", params, cbt)

    def req_handler_tincan_msg(self, cbt: CBT):
        lts = time.time()
        if cbt.request.params["Command"] == "LinkStateChange":
            lnkid = cbt.request.params["LinkId"]
            tnlid = cbt.request.params["TunnelId"]
            if (cbt.request.params["Data"] == "LINK_STATE_DOWN") and (
                self._tunnels[tnlid].tunnel_state != TUNNEL_STATES.QUERYING
            ):
                self.logger.debug("LINK %s STATE is DOWN cbt=%s", tnlid, cbt)
                # issue a link state check only if it not already being done
                self._tunnels[tnlid].tunnel_state = TUNNEL_STATES.QUERYING
                cbt.set_response(data=None, status=True)
                self.register_cbt("TincanTunnel", "TCI_QUERY_LINK_STATS", [tnlid])
            elif cbt.request.params["Data"] == "LINK_STATE_UP":
                tnlid = self.tunnel_id(lnkid)
                olid = self._tunnels[tnlid].overlay_id
                peer_id = self._tunnels[tnlid].peer_id
                lnk_status = self._tunnels[tnlid].tunnel_state
                self._tunnels[tnlid].tunnel_state = TUNNEL_STATES.ONLINE
                if lnk_status != TUNNEL_STATES.QUERYING:
                    param = {
                        "UpdateType": TUNNEL_EVENTS.Connected,
                        "OverlayId": olid,
                        "PeerId": peer_id,
                        "TunnelId": tnlid,
                        "LinkId": lnkid,
                        "ConnectedTimestamp": lts,
                        "TapName": self._tunnels[tnlid].tap_name,
                        "MAC": self._tunnels[tnlid].mac,
                        "PeerMac": self._tunnels[tnlid].peer_mac,
                        "Dataplane": self._tunnels[tnlid].dataplane,
                    }
                    self._link_updates_publisher.post_update(param)
                elif lnk_status == TUNNEL_STATES.QUERYING:
                    # Do not post a notification if the the connection state was being queried
                    self._tunnels[tnlid].link.status_retry = 0
            cbt.set_response(data=None, status=True)
        elif cbt.request.params["Command"] == "TincanReady":
            self.tc_session_id = cbt.request.params.get("SessionId", self.tc_session_id)
            cbt.set_response(data=None, status=True)
        elif cbt.request.params["Command"] == "ResetTincanTunnels":
            self.logger.info(
                "Clearing Tincan tunnels for session %s", self.tc_session_id
            )
            self._tunnels.clear()
            self._links.clear()
            self.tc_session_id = 0
            cbt.set_response(data=None, status=True)
        else:
            cbt.set_response(data=None, status=True)
        self.complete_cbt(cbt)

    def req_handler_query_tunnels_info(self, cbt: CBT):
        results = {}
        for tnlid in self._tunnels:
            if self._tunnels[tnlid].tunnel_state == TUNNEL_STATES.ONLINE:
                results[tnlid] = {
                    "OverlayId": self._tunnels[tnlid].overlay_id,
                    "TunnelId": tnlid,
                    "PeerId": self._tunnels[tnlid].peer_id,
                    "Stats": self._tunnels[tnlid].link.stats,
                    "TapName": self._tunnels[tnlid].tap_name,
                    "MAC": self._tunnels[tnlid].mac,
                    "PeerMac": self._tunnels[tnlid].peer_mac,
                }
        cbt.set_response(results, status=True)
        self.complete_cbt(cbt)

    def req_handler_remove_tnl(self, cbt: CBT):
        """Remove the tunnel and link given either the overlay id and peer id, or the tunnel id"""
        try:
            olid = cbt.request.params["OverlayId"]
            peer_id = cbt.request.params["PeerId"]
            tnlid = cbt.request.params["TunnelId"]
            if tnlid not in self._tunnels:
                cbt.set_response("No record", True)
                self.complete_cbt(cbt)
            elif (
                self._tunnels[tnlid].tunnel_state == TUNNEL_STATES.AUTHORIZED
                or self._tunnels[tnlid].tunnel_state == TUNNEL_STATES.ONLINE
                or self._tunnels[tnlid].tunnel_state == TUNNEL_STATES.OFFLINE
            ):
                tn = self._tunnels[tnlid].tap_name
                params = {
                    "OverlayId": olid,
                    "TunnelId": tnlid,
                    "PeerId": peer_id,
                    "TapName": tn,
                    "TincanId": self.tc_session_id,
                }
                self.register_cbt("TincanTunnel", "TCI_REMOVE_TUNNEL", params, cbt)
            else:
                cbt.set_response("Tunnel busy, retry operation", False)
                self.complete_cbt(cbt)
        except KeyError as err:
            cbt.set_response(f"Insufficient parameters {err}", False)
            self.complete_cbt(cbt)

    def _update_tunnel_descriptor(self, tnl_desc, tnlid):
        """
        Update the tunnel desc with with lock owned
        """
        self._tunnels[tnlid].mac = tnl_desc["MAC"]
        self._tunnels[tnlid].tap_name = tnl_desc["TapName"]
        self._tunnels[tnlid].fpr = tnl_desc["FPR"]

    def req_handler_add_ign_inf(self, cbt: CBT):
        ign_inf_details = cbt.request.params
        for olid in ign_inf_details:
            self._ignored_net_interfaces[olid] |= ign_inf_details[olid]
        cbt.set_response(None, True)
        self.complete_cbt(cbt)

    def req_handler_query_viz_data(self, cbt: CBT):
        tnls = dict()
        for tnlid in self._tunnels:
            if self._tunnels[tnlid].link is None:
                continue
            tnl_data = {}
            if self._tunnels[tnlid].tap_name:
                tnl_data["TapName"] = self._tunnels[tnlid].tap_name
            if self._tunnels[tnlid].mac:
                tnl_data["MAC"] = self._tunnels[tnlid].mac
            for stat_entry in self._tunnels[tnlid].link.stats:
                if stat_entry["best_conn"]:
                    lvals = stat_entry["local_candidate"].split(":")
                    rvals = stat_entry["remote_candidate"].split(":")
                    if len(lvals) < 10 or len(rvals) < 8:
                        continue
                    tnl_data["LocalEndpoint"] = {
                        "Proto": lvals[7],
                        "External": lvals[5] + ":" + lvals[6],
                        "Internal": lvals[8] + ":" + lvals[9],
                    }
                    tnl_data["RemoteEndpoint"] = {
                        "Proto": rvals[7],
                        "External": rvals[5] + ":" + rvals[6],
                    }
                    continue
            overlay_id = self._tunnels[tnlid].overlay_id
            if overlay_id not in tnls:
                tnls[overlay_id] = dict()
            tnls[overlay_id][tnlid] = tnl_data

        cbt.set_response({"LinkManager": tnls}, bool(tnls))
        self.complete_cbt(cbt)

    def resp_handler_create_link_endpt(self, cbt: CBT):
        """Create Link: Phase 4 Node B
        Create Link: Phase 6 Node A
        SIGnal to peer to update CAS
        Create Link: Phase 8 Node B
        Complete setup"""
        parent_cbt = cbt.parent
        resp_data = cbt.response.data
        if not cbt.response.status or parent_cbt is None:
            self.logger.warning(
                "Create link endpoint failed :%s or the parent expired", cbt
            )
            lnkid = cbt.request.params["LinkId"]
            self._rollback_link_creation_changes(lnkid)
            self.free_cbt(cbt)
            if parent_cbt:
                parent_cbt.set_response(resp_data, False)
                self.complete_cbt(parent_cbt)
            if resp_data and "CurrentId" in resp_data:
                self.tc_session_id = resp_data["CurrentId"]
            return

        if parent_cbt.request.action == "LNK_REQ_LINK_ENDPT":
            """
            To complete this request Node B has to supply its own
            NodeData and CAS. The NodeData was previously queried and is stored
            on the parent cbt. Add the cas and send to peer.
            """
            self._complete_link_endpt_request(cbt)

        elif parent_cbt.request.action == "LNK_CREATE_TUNNEL":
            """
            Both endpoints are created now but Node A must send its CAS. The peer
            (node B)already has the node data so no need to send that again.
            """
            self._send_local_cas_to_peer(cbt)

        elif parent_cbt.request.action == "LNK_ADD_PEER_CAS":
            """
            The link creation handshake is complete on Node B, complete the outstanding request
            and publish notifications via subscription.
            """
            self._complete_link_creation(cbt, parent_cbt)

    def resp_handler_remote_action(self, cbt: CBT):
        parent_cbt = cbt.parent
        resp_data = cbt.response.data
        rem_act: RemoteAction
        if not cbt.response.status or parent_cbt is None:
            rem_act = cbt.request.params
            lnkid = rem_act.params["LinkId"]
            tnlid = self.tunnel_id(lnkid)
            self.logger.debug(
                "The remote action requesting a connection endpoint link %s has failed or the parent expired",
                tnlid,
            )
            self._rollback_link_creation_changes(tnlid)
            self.free_cbt(cbt)
            if parent_cbt:
                parent_cbt.set_response(resp_data, False)
                self.complete_cbt(parent_cbt)
        else:
            rem_act = cbt.response.data
            self.free_cbt(cbt)
            if rem_act.action == "LNK_REQ_LINK_ENDPT":
                self._create_link_endpoint(rem_act, parent_cbt)
            elif rem_act.action == "LNK_ADD_PEER_CAS":
                self._complete_create_link_request(parent_cbt)
            else:
                self.logger("Unsupported Remote Action %s", rem_act)

    def resp_handler_create_tunnel(self, cbt: CBT):
        # Create Tunnel: Phase 2 Node A
        parent_cbt = cbt.parent
        lnkid = cbt.request.params["LinkId"]
        tnlid = cbt.request.params["TunnelId"]
        resp_data = cbt.response.data
        if not cbt.response.status or parent_cbt is None:
            self._deauth_tnl(tnlid)
            self.free_cbt(cbt)
            if parent_cbt:
                parent_cbt.set_response("Failed to create tunnel", False)
                self.complete_cbt(parent_cbt)
            self.logger.warning(
                "The create tunnel operation failed: %s or the parent expired",
                resp_data,
            )
            if resp_data and "CurrentId" in resp_data:
                self.tc_session_id = resp_data["CurrentId"]
            return
        # transistion connection connection state
        self._tunnels[tnlid].link.creation_state = 0xA2
        # store the overlay data
        overlay_id = cbt.request.params["OverlayId"]
        self.logger.debug("Create Link:%s Phase 2/5 Node A", lnkid[:7])
        self._update_tunnel_descriptor(resp_data, tnlid)
        # create and send remote action to request endpoint from peer
        params = {"OverlayId": overlay_id, "TunnelId": tnlid, "LinkId": lnkid}
        self._request_peer_endpoint(params, parent_cbt)
        self.free_cbt(cbt)

    def resp_handler_remove_tunnel(self, rmv_tnl_cbt: CBT):
        """
        Clean up the tunnel meta data. Even of the CBT fails it is safe to discard
        as this is because Tincan has no record of it.
        """
        parent_cbt = rmv_tnl_cbt.parent
        tnlid = rmv_tnl_cbt.request.params["TunnelId"]
        lnkid = self.link_id(tnlid)
        peer_id = rmv_tnl_cbt.request.params["PeerId"]
        olid = rmv_tnl_cbt.request.params["OverlayId"]
        tap_name = rmv_tnl_cbt.request.params["TapName"]
        resp_data = rmv_tnl_cbt.response.data
        if resp_data and "CurrentId" in resp_data:
            self.tc_session_id = resp_data["CurrentId"]
        self._tunnels.pop(tnlid, None)
        self._links.pop(lnkid, None)
        self.free_cbt(rmv_tnl_cbt)
        if parent_cbt:
            parent_cbt.set_response("Tunnel removed", True)
            self.complete_cbt(parent_cbt)
        # Notify subscribers of tunnel removal
        param = {
            "UpdateType": TUNNEL_EVENTS.Removed,
            "OverlayId": olid,
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "PeerId": peer_id,
            "TapName": tap_name,
        }
        self._link_updates_publisher.post_update(param)
        self.logger.info(
            "Tunnel %s removed: %s:%s<->%s",
            tnlid[:7],
            olid[:7],
            self.node_id[:7],
            peer_id[:7],
        )

    def resp_handler_query_link_stats(self, cbt: CBT):
        resp_data = cbt.response.data
        if not cbt.response.status:
            self.logger.warning("Link stats update error: %s", cbt.response.data)
            self.free_cbt(cbt)
            if resp_data and "CurrentId" in resp_data:
                self.tc_session_id = resp_data["CurrentId"]
            return
        # Handle any connection failures and update tracking data
        for tnlid in resp_data:
            for lnkid in resp_data[tnlid]:
                if resp_data[tnlid][lnkid]["Status"] == "UNKNOWN":
                    self._tunnels.pop(tnlid, None)
                elif tnlid in self._tunnels:
                    tnl = self._tunnels[tnlid]
                    if resp_data[tnlid][lnkid]["Status"] == "OFFLINE":
                        # tincan indicates offline so recheck the link status
                        retry = tnl.link.status_retry
                        if retry >= 2 and tnl.tunnel_state == TUNNEL_STATES.CREATING:
                            # link is stuck creating so destroy it
                            olid = tnl.overlay_id
                            peer_id = tnl.peer_id
                            params = {
                                "OverlayId": olid,
                                "TunnelId": tnlid,
                                "LinkId": lnkid,
                                "PeerId": peer_id,
                                "TapName": tnl.tap_name,
                                "TincanId": self.tc_session_id,
                            }
                            self.register_cbt(
                                "TincanTunnel", "TCI_REMOVE_TUNNEL", params
                            )
                        elif (tnl.tunnel_state == TUNNEL_STATES.QUERYING) or (
                            retry >= 1 and tnl.tunnel_state == TUNNEL_STATES.ONLINE
                        ):
                            # LINK_STATE_DOWN event or QUERY_LNK_STATUS response - post notify
                            tnl.tunnel_state = TUNNEL_STATES.OFFLINE
                            olid = tnl.overlay_id
                            peer_id = tnl.peer_id
                            param = {
                                "UpdateType": TUNNEL_EVENTS.Disconnected,
                                "OverlayId": olid,
                                "PeerId": peer_id,
                                "TunnelId": tnlid,
                                "LinkId": lnkid,
                                "TapName": tnl.tap_name,
                            }
                            self._link_updates_publisher.post_update(param)
                        else:
                            self.logger.warning(
                                "Link %s is offline, no further attempts to to query its stats will"
                                "be made.",
                                tnlid,
                            )
                    elif resp_data[tnlid][lnkid]["Status"] == "ONLINE":
                        tnl.tunnel_state = TUNNEL_STATES.ONLINE
                        tnl.link.stats = resp_data[tnlid][lnkid]["Stats"]
                        tnl.link.status_retry = 0
                    else:
                        self.logger.warning(
                            "Unrecognized tunnel state ",
                            "%s:%s",
                            lnkid,
                            resp_data[tnlid][lnkid]["Status"],
                        )
        self.free_cbt(cbt)

    def on_tnl_timeout(self, tnl: Tunnel, timeout: float):
        self._cleanup_failed_tunnel_data(tnl)

    def _register_abort_handlers(self):
        self._abort_handler_tbl = {
            "SIG_REMOTE_ACTION": self.abort_handler_tunnel,
            "TCI_CREATE_LINK": self.abort_handler_tunnel,
            "TCI_CREATE_TUNNEL": self.abort_handler_tunnel,
            "TCI_REMOVE_TUNNEL": self.abort_handler_tunnel,
            "TCI_QUERY_LINK_STATS": self.abort_handler_default,
            "TCI_REMOVE_LINK": self.abort_handler_default,
            "LNK_TUNNEL_EVENTS": self.abort_handler_default,
        }

    def _register_req_handlers(self):
        self._req_handler_tbl = {
            "LNK_CREATE_TUNNEL": self.req_handler_create_tunnel,
            "LNK_REQ_LINK_ENDPT": self.req_handler_req_link_endpt,
            "LNK_ADD_PEER_CAS": self.req_handler_add_peer_cas,
            "LNK_REMOVE_TUNNEL": self.req_handler_remove_tnl,
            "LNK_QUERY_TUNNEL_INFO": self.req_handler_query_tunnels_info,
            "VIS_DATA_REQ": self.req_handler_query_viz_data,
            "TCI_TINCAN_MSG_NOTIFY": self.req_handler_tincan_msg,
            "LNK_ADD_IGN_INF": self.req_handler_add_ign_inf,
            "LNK_AUTH_TUNNEL": self.req_handler_auth_tunnel,
        }

    def _register_resp_handlers(self):
        self._resp_handler_tbl = {
            "SIG_REMOTE_ACTION": self.resp_handler_remote_action,
            "TCI_CREATE_LINK": self.resp_handler_create_link_endpt,
            "TCI_CREATE_TUNNEL": self.resp_handler_create_tunnel,
            "TCI_QUERY_LINK_STATS": self.resp_handler_query_link_stats,
            "TCI_REMOVE_TUNNEL": self.resp_handler_remove_tunnel,
        }

    def _gen_tap_name(self, overlay_id: str, peer_id: str) -> str:
        tap_name_prefix = self.config["Overlays"][overlay_id].get(
            "TapNamePrefix", overlay_id[:5]
        )
        end_i = self.TAPNAME_MAXLEN - len(tap_name_prefix)
        tap_name = tap_name_prefix + str(peer_id[:end_i])
        return tap_name

    def _get_ignored_tap_names(self, overlay_id, new_inf_name=None):
        ign_netinf = set()
        if new_inf_name:
            ign_netinf.add(new_inf_name)

        if not self.config["Overlays"][overlay_id].get(
            "AllowRecursiveTunneling", False
        ):
            # Ignore ALL the evio tap devices (regardless of their overlay id/link id)
            for tnlid in self._tunnels:
                if self._tunnels[tnlid].tap_name:
                    ign_netinf.add(self._tunnels[tnlid].tap_name)
        # add the global ignore list
        ign_netinf.update(self.config.get("IgnoredNetInterfaces", []))
        # add the overlay specifc list
        ign_netinf |= self._ignored_net_interfaces[overlay_id]
        return ign_netinf

    def is_link_completed(self, tnl: Tunnel) -> bool:
        return bool(tnl.link and tnl.link.creation_state == 0xC0)

    def _query_link_stats(self):
        """Query the status of links that have completed creation process"""
        params = []
        for tnlid in self._tunnels:
            link = self._tunnels[tnlid].link
            if link and link.creation_state == 0xC0:
                params.append(tnlid)
        if params:
            self.register_cbt("TincanTunnel", "TCI_QUERY_LINK_STATS", params)

    def _remove_link_from_tunnel(self, tnlid):
        tnl = self._tunnels.get(tnlid)
        if tnl:
            if tnl.link and tnl.link.lnkid:
                self._links.pop(tnl.link.lnkid)
            tnl.link = None
            tnl.tunnel_state = TUNNEL_STATES.OFFLINE

    def link_id(self, tnlid):
        tnl = self._tunnels.get(tnlid, None)
        if tnl and tnl.link:
            return tnl.link.lnkid
        return None

    def tunnel_id(self, lnkid):
        return self._links.get(lnkid)

    def _assign_link_to_tunnel(self, tnlid, lnkid, state):
        if tnlid in self._tunnels:
            self._tunnels[tnlid].link = Link(lnkid, state)
        self._links[lnkid] = tnlid

    def _create_tunnel(self, params, parent_cbt):
        overlay_id = params["OverlayId"]
        tnlid = params["TunnelId"]
        lnkid = params["LinkId"]
        peer_id = params["PeerId"]
        tap_name = self._gen_tap_name(overlay_id, peer_id)
        self.logger.debug(
            "IgnoredNetInterfaces: %s",
            self._get_ignored_tap_names(overlay_id, tap_name),
        )
        create_tnl_params = {
            "OverlayId": overlay_id,
            "NodeId": self.node_id,
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "StunServers": self.config.get("Stun", []),
            "TapName": tap_name,
            "IgnoredNetInterfaces": list(
                self._get_ignored_tap_names(overlay_id, tap_name)
            ),
            "TincanId": self.tc_session_id,
        }
        if self.config.get("Turn"):
            create_tnl_params["TurnServers"] = self.config["Turn"]

        self.register_cbt(
            "TincanTunnel", "TCI_CREATE_TUNNEL", create_tnl_params, parent_cbt
        )

    def _request_peer_endpoint(self, params: dict, parent_cbt: CBT):
        overlay_id = params["OverlayId"]
        tnlid = params["TunnelId"]
        endp_param = {
            "NodeData": {
                "FPR": self._tunnels[tnlid].fpr,
                "MAC": self._tunnels[tnlid].mac,
                "UID": self.node_id,
            }
        }
        endp_param.update(params)
        rem_act = RemoteAction(
            overlay_id=overlay_id,
            recipient_id=parent_cbt.request.params["PeerId"],
            recipient_cm="LinkManager",
            action="LNK_REQ_LINK_ENDPT",
            params=endp_param,
        )
        rem_act.submit_remote_act(self, parent_cbt)

    def _create_link_endpoint(self, rem_act: RemoteAction, parent_cbt: CBT):
        """
        Send the Create Link control to local Tincan to initiate link NAT traversal
        """
        # Create Link: Phase 5 Node A
        lnkid = rem_act.data["LinkId"]
        tnlid = self.tunnel_id(lnkid)
        peer_id = rem_act.recipient_id
        if tnlid not in self._tunnels:
            # abort the handshake as the process timed out
            parent_cbt.set_response("Tunnel creation timeout failure", False)
            self.complete_cbt(parent_cbt)
            return
        self._tunnels[tnlid].link.creation_state = 0xA3
        self.logger.debug(
            "Create Link:%s Phase 3/5 Node A - Peer: %s", lnkid[:7], peer_id[:7]
        )
        node_data = rem_act.data["NodeData"]
        olid = rem_act.overlay_id
        # add the peer MAC to the tunnel descr
        self._tunnels[tnlid].peer_mac = node_data["MAC"]
        cbt_params = {
            "OverlayId": olid,
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "NodeData": {
                "UID": node_data["UID"],
                "MAC": node_data["MAC"],
                "CAS": node_data["CAS"],
                "FPR": node_data["FPR"],
            },
            "TincanId": self._tunnels[tnlid].dp_instance_id,
        }
        self.register_cbt("TincanTunnel", "TCI_CREATE_LINK", cbt_params, parent_cbt)

    def _complete_create_link_request(self, parent_cbt: CBT):
        # Create Link: Phase 9 Node A
        # Complete the cbt that started this all
        olid = parent_cbt.request.params["OverlayId"]
        peer_id = parent_cbt.request.params["PeerId"]
        tnlid = parent_cbt.request.params["TunnelId"]
        if tnlid not in self._tunnels:
            # abort the handshake as the process timed out
            parent_cbt.set_response("Tunnel creation timeout failure", False)
            self.complete_cbt(parent_cbt)
            return
        lnkid = self.link_id(tnlid)
        self._tunnels[tnlid].link.creation_state = 0xC0
        self.logger.debug(
            "Create Link:%s Phase 5/5 Node A - Peer: %s", tnlid[:7], peer_id[:7]
        )
        parent_cbt.set_response(data={"LinkId": lnkid}, status=True)
        self.complete_cbt(parent_cbt)
        self.logger.debug(
            "Tunnel %s created: %s:%s->%s",
            lnkid[:7],
            olid[:7],
            self.node_id[:7],
            peer_id[:7],
        )

    def _complete_link_endpt_request(self, cbt: CBT):
        # Create Link: Phase 4 Node B
        parent_cbt = cbt.parent
        resp_data = cbt.response.data
        lnkid = cbt.request.params["LinkId"]
        tnlid = self.tunnel_id(lnkid)
        peer_id = cbt.request.params["NodeData"]["UID"]
        if not cbt.response.status:
            self.free_cbt(cbt)
            parent_cbt.set_response(resp_data, False)
            if parent_cbt and parent_cbt.child_count == 0:
                self.complete_cbt(parent_cbt)
            self.logger.warning(
                "Failed to create connection endpoint for request link: %s. Response data= %s",
                lnkid,
                cbt.response.data,
            )
            self._rollback_link_creation_changes(tnlid)
            return
        self.logger.debug(
            "Create Link:%s Phase 2/4 Node B - Peer: %s", lnkid[:7], peer_id[:7]
        )
        self._tunnels[tnlid].link.creation_state = 0xB2
        # store the overlay data
        self._update_tunnel_descriptor(resp_data, tnlid)
        # add the peer MAC to the tunnel descr
        node_data = cbt.request.params["NodeData"]
        self._tunnels[tnlid].peer_mac = node_data["MAC"]
        # respond with this nodes connection parameters
        node_data = {
            "MAC": resp_data["MAC"],
            "FPR": resp_data["FPR"],
            "UID": self.node_id,
            "CAS": resp_data["CAS"],
        }
        data = {
            "OverlayId": cbt.request.params["OverlayId"],
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "NodeData": node_data,
        }
        self.free_cbt(cbt)
        parent_cbt.set_response(data, True)
        self.complete_cbt(parent_cbt)

    def _complete_link_creation(self, cbt, parent_cbt):
        params = parent_cbt.request.params
        lnkid = params["LinkId"]
        tnlid = self.tunnel_id(lnkid)
        peer_id = params["NodeData"]["UID"]
        self._tunnels[tnlid].link.creation_state = 0xC0
        self.logger.debug(
            "Create Link:%s Phase 4/4 Node B - Peer: %s", lnkid[:7], peer_id[:7]
        )
        peer_id = params["NodeData"]["UID"]
        olid = params["OverlayId"]
        resp_data = cbt.response.data
        node_data = {
            "MAC": resp_data["MAC"],
            "FPR": resp_data["FPR"],
            "UID": self.node_id,
            "CAS": resp_data["CAS"],
        }
        data = {
            "OverlayId": cbt.request.params["OverlayId"],
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "NodeData": node_data,
        }
        parent_cbt.set_response(data=data, status=True)
        self.free_cbt(cbt)
        self.complete_cbt(parent_cbt)
        self.logger.info(
            "Tunnel %s Link %s accepted: %s:%s<-%s",
            tnlid[:7],
            lnkid[:7],
            olid[:7],
            self.node_id[:7],
            peer_id[:7],
        )

    def _send_local_cas_to_peer(self, cbt: CBT):
        # Create Link: Phase 6 Node A
        lnkid = cbt.request.params["LinkId"]
        tnlid = self.tunnel_id(lnkid)
        peer_id = cbt.request.params["NodeData"]["UID"]
        self._tunnels[tnlid].link.creation_state = 0xA4
        self.logger.debug(
            "Create Link:%s Phase 4/5 Node A - Peer: %s", lnkid[:7], peer_id[:7]
        )
        local_cas = cbt.response.data["CAS"]
        parent_cbt = cbt.parent
        olid = cbt.request.params["OverlayId"]
        peerid = parent_cbt.request.params["PeerId"]
        params = {
            "OverlayId": olid,
            "TunnelId": tnlid,
            "LinkId": lnkid,
            "NodeData": {
                "UID": self.node_id,
                "MAC": cbt.response.data["MAC"],
                "CAS": local_cas,
                "FPR": cbt.response.data["FPR"],
            },
        }
        self.free_cbt(cbt)
        rem_act = RemoteAction(
            overlay_id=olid,
            recipient_id=peerid,
            recipient_cm="LinkManager",
            action="LNK_ADD_PEER_CAS",
            params=params,
        )
        rem_act.submit_remote_act(self, parent_cbt)

    def _deauth_tnl(self, tnlid: str):
        tnl = self._tunnels.get(tnlid)
        if not tnl:
            return
        self.logger.info("Deauthorizing tunnel %s", tnlid)
        param = {
            "UpdateType": TUNNEL_EVENTS.AuthExpired,
            "OverlayId": tnl.overlay_id,
            "PeerId": tnl.peer_id,
            "TunnelId": tnlid,
            "TapName": tnl.tap_name,
        }
        self._link_updates_publisher.post_update(param)
        self._cleanup_failed_tunnel_data(tnl)

    def _rollback_link_creation_changes(self, tnlid):
        """
        Removes links that failed the setup handshake.
        """
        if tnlid not in self._tunnels:
            return
        tnl = self._tunnels[tnlid]
        if tnl.link:
            creation_state = tnl.link.creation_state
            if creation_state < 0xC0:
                olid = self._tunnels[tnlid].overlay_id
                peer_id = self._tunnels[tnlid].peer_id
                lnkid = self.link_id(tnlid)
                params = {
                    "OverlayId": olid,
                    "PeerId": peer_id,
                    "TunnelId": tnlid,
                    "LinkId": lnkid,
                    "TapName": self._tunnels[tnlid].tap_name,
                    "TincanId": self.tc_session_id,
                }
                self.logger.info(
                    "Initiating removal of incomplete link: "
                    "PeerId: %s, LinkId: %s, CreateState: %s",
                    peer_id[:7],
                    tnlid[:7],
                    format(creation_state, "02X"),
                )
                self.register_cbt("TincanTunnel", "TCI_REMOVE_TUNNEL", params)
            self._cleanup_failed_tunnel_data(tnl)

    def _cleanup_failed_tunnel_data(self, tnl: Tunnel):
        self.logger.debug("Removing failed tunnel data %s", tnl)
        if tnl:
            self._tunnels.pop(tnl.tnlid, None)
            lnkid = self.link_id(tnl.tnlid)
            self._links.pop(lnkid, None)


"""
###################################################################################################
Link Manager state and event specifications
###################################################################################################

If LM fails a CBT there will be no further events fired for the tunnel.
Once tunnel goes online an explicit CBT LNK_REMOVE_TUNNEL is required.
Partially created tunnels that fails will be removed automatically by LM.

Events
(1) TunnelEvents.AuthExpired - After a successful completion of CBT LNK_AUTH_TUNNEL, the tunnel
descriptor is created and TunnelEvents.Authorized is fired.
(2) TunnelEvents.AuthExpired - If no action is taken on the tunnel within LinkSetupTimeout LM will
fire TunnelEvents.AuthExpired and remove the associated tunnel descriptor.
(3) ##REMOVED## TunnelEvents.Created - On both nodes A & B, on a successful completion of CBT
TCI_CREATE_TUNNEL, the TAP device exists and TunnelEvents.Created is fired.
(4) TunnelEvents.Connected - After Tincan delivers the online event to LM TunnelEvents.Connected
is fired.
(5) TunnelEvents.Disconnected - After Tincan signals link offline or QUERYy_LNK_STATUS discovers
offline TunnelEvents.Disconnected is fired.
(6) TunnelEvents.Removed - After the TAP device is removed TunnelEvents.Removed is fired and the
tunnel descriptor is removed. Tunnel must be in TUNNEL_STATES.ONLINE or TUNNEL_STATES.OFFLINE

 Internal States
(1) TUNNEL_STATES.AUTHORIZED - After a successful completion of CBT LNK_AUTH_TUNNEL, the tunnel
descriptor exists.
(2) TUNNEL_STATES.CREATING - entered on reception of CBT LNK_CREATE_TUNNEL.
(3) TUNNEL_STATES.QUERYING - entered before issuing CBT TCI_QUERY_LINK_STATS. Happens when
LinkStateChange is LINK_STATE_DOWN and state is not already TUNNEL_STATES.QUERYING; OR
TCI_QUERY_LINK_STATS is OFFLINE and state is not already TUNNEL_STATES.QUERYING.
(4) TUNNEL_STATES.ONLINE - entered when CBT TCI_QUERY_LINK_STATS is ONLINE or LinkStateChange is
LINK_STATE_UP.
(5) TUNNEL_STATES.OFFLINE - entered when QUERY_LNK_STATUS is OFFLINE or LinkStateChange is
LINK_STATE_DOWN event.
"""
