import asyncio
import json
import uuid
import time
import websockets
import logging
import hmac
import argparse
import os
from typing import Any, Dict, List, Optional

try:
    from core.src.messaging.control_plane import BaseControlPlane
    from core.src.security.signer import MessageSigner
    from core.src.messaging.protocol import Message, MessageHeader, MessagePayload
except ImportError:
    from messaging.control_plane import BaseControlPlane
    from security.signer import MessageSigner
    from messaging.protocol import Message, MessageHeader, MessagePayload

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("KVMControlPlane")


class KVMControlPlane(BaseControlPlane):
    """
    Control plane for the KVM hypervisor spoke.

    Implements the same multi-agent pattern as PxmxControlPlane:
      - Listens on port 8767 for KVM agents (libvirt-agent processes)
      - Authenticates agents with a shared agent_secret
      - Aggregates telemetry; fans out commands via send_to_agent / broadcast_to_agents
      - Registers as module_type="hypervisor" so the hub treats it identically to pxmx
    """

    AGENT_PORT = 8767

    def get_service_name(self) -> str:
        return "lm-kvm"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "hypervisor"

        config_path = "/etc/lm-kvm/config.json"
        self.config: Dict[str, Any] = {}
        try:
            if os.path.exists(config_path):
                with open(config_path) as f:
                    self.config = json.load(f)
        except Exception as e:
            logger.error(f"Could not load KVM config: {e}")

        self.agent_secret: Optional[str] = self.config.get("agent_secret")
        if not self.agent_secret:
            logger.warning("agent_secret not set — agent auth will fail (fail-closed)")

        self.agent_signer = MessageSigner(self.agent_secret or "")
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.connected_agents: Dict[str, Dict[str, Any]] = {}

    # ── Agent WebSocket server ────────────────────────────────────────────────

    async def run_agent_server(self):
        async with websockets.serve(self._agent_handler, "0.0.0.0", self.AGENT_PORT):
            logger.info(f"KVM agent listener on :{self.AGENT_PORT}")
            await asyncio.Future()

    async def _agent_handler(self, websocket, path=None):
        agent_id = None
        try:
            auth = json.loads(await websocket.recv())
            agent_id     = auth.get("agent_id")
            agent_secret = auth.get("secret")

            if not agent_id or not agent_secret:
                await websocket.close(1008, "Missing credentials"); return

            if not self.agent_secret or not hmac.compare_digest(str(agent_secret), str(self.agent_secret)):
                logger.warning(f"Agent {agent_id} auth failed")
                await websocket.close(1008, "Auth failed"); return

            await websocket.send(json.dumps({"status": "HUB_VERIFIED"}))
            ack = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            if ack.get("status") != "HUB_OK":
                await websocket.close(1008, "Mutual auth failed"); return

            logger.info(f"KVM agent '{agent_id}' connected")
            self.connected_agents[agent_id] = {
                "ws":           websocket,
                "hostname":     agent_id,
                "last_seen":    time.time(),
                "nodes":        [],
                "vms":          [],
                "agent_metrics": {},
            }

            async for raw in websocket:
                msg = json.loads(raw)
                if "signature" not in msg or not self.agent_signer.verify(msg):
                    logger.warning("Invalid agent message signature — dropping"); continue

                payload  = msg.get("payload", {})
                msg_type = payload.get("type")
                data     = payload.get("data", {})
                corr_id  = msg.get("header", {}).get("correlation_id")

                if msg_type == "AGENT_HEARTBEAT":
                    if agent_id in self.connected_agents:
                        self.connected_agents[agent_id]["last_seen"] = time.time()

                elif msg_type == "AGENT_TELEMETRY":
                    if agent_id in self.connected_agents:
                        rec = self.connected_agents[agent_id]
                        rec["last_seen"]    = time.time()
                        rec["hostname"]     = data.get("hostname", agent_id)
                        rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
                        rec["vms"]          = data.get("vms",   {}).get("vms",   [])
                        rec["agent_metrics"] = data.get("metrics", {})
                    if "kvm" in self.modules and hasattr(self.modules["kvm"], "telemetry_cache"):
                        self.modules["kvm"].telemetry_cache[agent_id] = data

                elif msg_type == "AGENT_RESPONSE":
                    if corr_id in self.pending_responses:
                        fut = self.pending_responses.pop(corr_id)
                        if not fut.done():
                            fut.set_result(data)

                elif msg_type == "AGENT_LOG":
                    relay = Message(
                        header=MessageHeader(message_id=str(uuid.uuid4()), timestamp=time.time(),
                                            sender_id=self.spoke_id, destination_id="hub"),
                        payload=MessagePayload(type="AGENT_RELAY_UP",
                                               data={"agent_id": agent_id, "original_payload": msg}))
                    await self.send_to_hub(relay)

        except Exception as e:
            logger.error(f"KVM agent handler error: {e}", exc_info=True)
        finally:
            if agent_id and agent_id in self.connected_agents:
                del self.connected_agents[agent_id]
            logger.info(f"KVM agent '{agent_id}' disconnected")

    # ── Agent command routing ─────────────────────────────────────────────────

    async def send_to_agent(self, cmd_type: str, data: Dict[str, Any],
                            agent_id: Optional[str] = None) -> Dict[str, Any]:
        if agent_id:
            rec = self.connected_agents.get(agent_id)
            if not rec:
                return {"status": "ERROR", "message": f"Agent '{agent_id}' not connected"}
            ws = rec["ws"]
        else:
            if not self.connected_agents:
                return {"status": "ERROR", "message": "No KVM agents connected"}
            ws = next(iter(self.connected_agents.values()))["ws"]

        corr_id = str(uuid.uuid4())
        msg = {
            "header": {"message_id": corr_id, "timestamp": time.time(),
                       "sender_id": self.spoke_id, "destination_id": agent_id or "kvm-agent"},
            "payload": {"type": cmd_type, "data": data},
        }
        msg["signature"] = self.agent_signer.sign(msg)
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[corr_id] = fut
        try:
            await ws.send(json.dumps(msg, separators=(',', ':')))
            return await asyncio.wait_for(fut, timeout=15.0)
        except asyncio.TimeoutError:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": "Agent response timeout"}
        except Exception as e:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": str(e)}

    async def broadcast_to_agents(self, cmd_type: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.connected_agents:
            return []
        results = await asyncio.gather(
            *[self.send_to_agent(cmd_type, data, aid) for aid in list(self.connected_agents)],
            return_exceptions=True,
        )
        return [
            {"agent_id": aid, **(res if not isinstance(res, Exception) else {"status": "ERROR", "message": str(res)})}
            for aid, res in zip(self.connected_agents, results)
        ]

    # ── Spoke startup ─────────────────────────────────────────────────────────

    async def run(self):
        logger.info(f"Starting KVM spoke → {self.hub_url}")
        asyncio.create_task(self.run_agent_server())

        from kvm_spoke import KVMSpoke
        kvm_spoke = KVMSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("kvm", kvm_spoke)

        await super().run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--id",         required=True)
    parser.add_argument("--secret",     required=True)
    parser.add_argument("--hub-secret", default="")
    parser.add_argument("--hub",        required=True)
    args = parser.parse_args()

    cp = KVMControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())
