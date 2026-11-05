"""Control plane for the KVM hypervisor spoke.

Connects to the Lab Manager hub over WebSocket (via BaseControlPlane), runs a
local WebSocket server that libvirt agents dial into, authenticates those
agents, tracks their telemetry and relays hub commands to them.
"""
# asyncio drives the WebSocket servers; json/uuid/time build and parse messages.
import asyncio
import json
import uuid
import time
# websockets serves the agent listener; logging reports activity.
import websockets
import logging
# hmac gives a constant-time secret comparison; argparse/os handle CLI and files.
import hmac
import argparse
import os
# Type hints only.
from typing import Any, Dict, List, Optional

# Shared hub-link base class, message signer and message types; the import path
# differs between the repo layout (core.src...) and the installed flat layout.
try:
    from core.src.messaging.control_plane import BaseControlPlane
    from core.src.security.signer import MessageSigner
    from core.src.messaging.protocol import Message, MessageHeader, MessagePayload
except ImportError:
    from messaging.control_plane import BaseControlPlane
    from security.signer import MessageSigner
    from messaging.protocol import Message, MessageHeader, MessagePayload

# Shared logging setup + a canonical local file log (contract req 6 +
# normalization). configure_logging gives the standard format + LOG_LEVEL env;
# log_file makes a Python FileHandler own /var/log/lm/kvm.log (the canonical
# location the hub scans), with a local fallback for a hand-run. Mirrors pxmx.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        # Neither shared helper is available: define a minimal equivalent here.
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=logging.INFO, *, log_file=None, **_):
            """Fallback logging setup: console output, plus a log file when one is given."""
            # Log to both a file and the console only when a log file was requested.
            handlers = ([logging.FileHandler(log_file), logging.StreamHandler()]
                        if log_file else None)
            # force=True replaces any handlers configured earlier.
            logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)


def _resolve_log_file(name: str):
    """Canonical /var/log/lm/<name>.log if writable, else a local logs/ fallback,
    else None (stderr only)."""
    # Preferred location: the directory the hub scans for spoke logs.
    primary = f"/var/log/lm/{name}.log"
    # Create the directory and prove the file is writable by opening it for append.
    try:
        os.makedirs("/var/log/lm", exist_ok=True)
        with open(primary, "a"):
            pass
        return primary
    # Not writable (e.g. hand-run as a normal user): use a logs/ folder beside this file.
    except OSError:
        try:
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(local, exist_ok=True)
            return os.path.join(local, f"{name}.log")
        # Neither location is writable: log to stderr only.
        except OSError:
            return None


# Configure logging (standard format, canonical log file) before anything logs.
configure_logging(log_file=_resolve_log_file("kvm"))
# One named logger for this control plane.
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

    # TCP port the local KVM agents connect to.
    AGENT_PORT = 8767

    def get_service_name(self) -> str:
        """Return the systemd service name reported to the hub for health checks."""
        return "lm-kvm"

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None, hub_url: str = None):
        """Set up hub identity, load the local KVM config and prepare agent tracking.

        Args:
            spoke_id: This spoke's identity at the hub.
            secret: Shared secret used to authenticate with the hub.
            hub_secret: Optional hub secret for mutual authentication.
            hub_url: WebSocket URL of the hub.
        """
        # Let the base class set up identity, secrets and the hub URL.
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        # The hub treats this spoke as a hypervisor, the same as pxmx.
        self.module_type = "hypervisor"

        # Local config written by the installer; missing or unreadable means defaults.
        config_path = "/etc/lm-kvm/config.json"
        self.config: Dict[str, Any] = {}
        # Load the JSON config when the file exists.
        try:
            if os.path.exists(config_path):
                with open(config_path) as f:
                    self.config = json.load(f)
        # Bad JSON or permissions: log it and continue with an empty config.
        except Exception as e:
            logger.error(f"Could not load KVM config: {e}")

        # Secret agents must present; without it every agent is rejected (fail closed).
        self.agent_secret: Optional[str] = self.config.get("agent_secret")
        if not self.agent_secret:
            logger.warning("agent_secret not set — agent auth will fail (fail-closed)")

        # Signer for agent messages, pending command futures and connected agent records.
        self.agent_signer = MessageSigner(self.agent_secret or "")
        self.pending_responses: Dict[str, asyncio.Future] = {}
        self.connected_agents: Dict[str, Dict[str, Any]] = {}

    # ── Agent WebSocket server ────────────────────────────────────────────────

    async def run_agent_server(self):
        """Listen for KVM agent connections on AGENT_PORT forever."""
        # Serve agents on all interfaces; each connection is handled by _agent_handler.
        async with websockets.serve(self._agent_handler, "0.0.0.0", self.AGENT_PORT):
            logger.info(f"KVM agent listener on :{self.AGENT_PORT}")
            # Await a future that never completes so the server runs until cancelled.
            await asyncio.Future()

    async def _agent_handler(self, websocket, path=None):
        """Authenticate one agent connection, then process its messages until it closes."""
        # Unknown until the agent introduces itself.
        agent_id = None
        try:
            # The first message must carry the agent id and shared secret.
            auth = json.loads(await websocket.recv())
            agent_id     = auth.get("agent_id")
            agent_secret = auth.get("secret")

            # Missing credentials: close with a policy-violation code.
            if not agent_id or not agent_secret:
                await websocket.close(1008, "Missing credentials"); return

            # Compare secrets in constant time; any mismatch (or no secret configured) is rejected.
            if not self.agent_secret or not hmac.compare_digest(str(agent_secret), str(self.agent_secret)):
                logger.warning(f"Agent {agent_id} auth failed")
                await websocket.close(1008, "Auth failed"); return

            # Mutual handshake: tell the agent it is verified and wait up to 5s for its OK.
            await websocket.send(json.dumps({"status": "HUB_VERIFIED"}))
            ack = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
            if ack.get("status") != "HUB_OK":
                await websocket.close(1008, "Mutual auth failed"); return

            # Register the agent with empty telemetry until its first report arrives.
            logger.info(f"KVM agent '{agent_id}' connected")
            self.connected_agents[agent_id] = {
                "ws":           websocket,
                "hostname":     agent_id,
                "last_seen":    time.time(),
                "nodes":        [],
                "vms":          [],
                "agent_metrics": {},
            }

            # Process every message the agent sends.
            async for raw in websocket:
                # Drop any message whose signature is missing or invalid.
                msg = json.loads(raw)
                if "signature" not in msg or not self.agent_signer.verify(msg):
                    logger.warning("Invalid agent message signature — dropping"); continue

                # Split the message into its type, data and correlation id.
                payload  = msg.get("payload", {})
                msg_type = payload.get("type")
                data     = payload.get("data", {})
                corr_id  = msg.get("header", {}).get("correlation_id")

                # Heartbeat: just refresh the last-seen time.
                if msg_type == "AGENT_HEARTBEAT":
                    if agent_id in self.connected_agents:
                        self.connected_agents[agent_id]["last_seen"] = time.time()

                # Telemetry: refresh last-seen and store the host's nodes, VMs and metrics.
                elif msg_type == "AGENT_TELEMETRY":
                    if agent_id in self.connected_agents:
                        rec = self.connected_agents[agent_id]
                        rec["last_seen"]    = time.time()
                        rec["hostname"]     = data.get("hostname", agent_id)
                        rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
                        rec["vms"]          = data.get("vms",   {}).get("vms",   [])
                        rec["agent_metrics"] = data.get("metrics", {})

                # Response to a command: resolve the waiting future with its data.
                elif msg_type == "AGENT_RESPONSE":
                    if corr_id in self.pending_responses:
                        fut = self.pending_responses.pop(corr_id)
                        if not fut.done():
                            fut.set_result(data)

                # Log line: wrap it and relay it up to the hub.
                elif msg_type == "AGENT_LOG":
                    relay = Message(
                        header=MessageHeader(message_id=str(uuid.uuid4()), timestamp=time.time(),
                                            sender_id=self.spoke_id, destination_id="hub"),
                        payload=MessagePayload(type="AGENT_RELAY_UP",
                                               data={"agent_id": agent_id, "original_payload": msg}))
                    await self.send_to_hub(relay)

        # Any error ends this connection; log it with the traceback.
        except Exception as e:
            logger.error(f"KVM agent handler error: {e}", exc_info=True)
        # Always forget the agent once its connection ends.
        finally:
            if agent_id and agent_id in self.connected_agents:
                del self.connected_agents[agent_id]
            logger.info(f"KVM agent '{agent_id}' disconnected")

    # ── Agent command routing ─────────────────────────────────────────────────

    async def send_to_agent(self, cmd_type: str, data: Dict[str, Any],
                            agent_id: Optional[str] = None) -> Dict[str, Any]:
        """Send a command to one agent (or the first connected one) and await its reply.

        Returns:
            The agent's response data, or an ERROR dict on missing agent/timeout/failure.
        """
        # A specific agent was requested: it must be connected.
        if agent_id:
            rec = self.connected_agents.get(agent_id)
            if not rec:
                return {"status": "ERROR", "message": f"Agent '{agent_id}' not connected"}
            ws = rec["ws"]
        else:
            # No agent named: use the first connected one, if any.
            if not self.connected_agents:
                return {"status": "ERROR", "message": "No KVM agents connected"}
            ws = next(iter(self.connected_agents.values()))["ws"]

        # Build the command message with a fresh correlation id and sign it.
        corr_id = str(uuid.uuid4())
        msg = {
            "header": {"message_id": corr_id, "timestamp": time.time(),
                       "sender_id": self.spoke_id, "destination_id": agent_id or "kvm-agent"},
            "payload": {"type": cmd_type, "data": data},
        }
        msg["signature"] = self.agent_signer.sign(msg)
        # Register a future that _agent_handler resolves when the reply arrives.
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[corr_id] = fut
        # Send the command and wait up to 15s for the reply.
        try:
            await ws.send(json.dumps(msg, separators=(',', ':')))
            return await asyncio.wait_for(fut, timeout=15.0)
        # No reply in time: forget the future and report a timeout.
        except asyncio.TimeoutError:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": "Agent response timeout"}
        # Send failure: forget the future and report the error.
        except Exception as e:
            self.pending_responses.pop(corr_id, None)
            return {"status": "ERROR", "message": str(e)}

    async def broadcast_to_agents(self, cmd_type: str, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Send a command to every connected agent in parallel and collect the replies."""
        # No agents means no replies.
        if not self.connected_agents:
            return []
        # Run all sends concurrently; exceptions are returned instead of raised.
        results = await asyncio.gather(
            *[self.send_to_agent(cmd_type, data, aid) for aid in list(self.connected_agents)],
            return_exceptions=True,
        )
        # Pair each reply with its agent id, turning exceptions into ERROR entries.
        return [
            {"agent_id": aid, **(res if not isinstance(res, Exception) else {"status": "ERROR", "message": str(res)})}
            for aid, res in zip(self.connected_agents, results)
        ]

    # ── Spoke startup ─────────────────────────────────────────────────────────

    async def run(self):
        """Start the agent listener, register the KVM module and run the hub link."""
        # Announce the hub target and start the agent server in the background.
        logger.info(f"Starting KVM spoke → {self.hub_url}")
        asyncio.create_task(self.run_agent_server())

        # Imported here to avoid a circular import; the spoke needs this control plane.
        from kvm_spoke import KVMSpoke
        kvm_spoke = KVMSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("kvm", kvm_spoke)

        # Hand over to the base class connection/heartbeat/dispatch loop.
        await super().run()


if __name__ == "__main__":
    # Command-line flags: spoke identity, secrets and the (required) hub URL.
    parser = argparse.ArgumentParser()
    parser.add_argument("--id",         required=True)
    parser.add_argument("--secret",     required=True)
    parser.add_argument("--hub-secret", default="")
    parser.add_argument("--hub",        required=True)
    # Read the flags the user passed.
    args = parser.parse_args()

    # Build the control plane and run it until the process is stopped.
    cp = KVMControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())
