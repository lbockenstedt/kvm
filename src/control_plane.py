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
# ssl wraps the agent listener in TLS (the agent secret travels in its first frame).
import ssl
import subprocess
# Type hints only.
from typing import Any, Dict, List, Optional, Tuple

# Shared hub-link base class and message signer; the import path differs
# between the repo layout (core.src...) and the installed flat layout.
try:
    from core.src.messaging.control_plane import BaseControlPlane
    from core.src.security.signer import MessageSigner
except ImportError:
    from messaging.control_plane import BaseControlPlane
    from security.signer import MessageSigner

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
    # Seconds an unauthenticated connection may take to send its auth frame.
    AGENT_AUTH_TIMEOUT = 10.0
    # WebSocket keepalive: a connection that misses a pong for PING_TIMEOUT
    # seconds is closed, so a dead agent's agent_id is freed within about
    # PING_INTERVAL + PING_TIMEOUT seconds (see the duplicate-id check).
    AGENT_PING_INTERVAL = 20.0
    AGENT_PING_TIMEOUT = 20.0

    @staticmethod
    def _repo_identity(url: str) -> str:
        """Return ``host/owner/repo`` for a git URL (https, ssh or scp form),
        lower-cased, without credentials, port or ``.git``; "" if unparseable.

        The whole identity is compared, not just the repo name, so a
        same-named fork on another owner or host is a different repository."""
        text = (url or "").strip().rstrip("/")
        # Drop the scheme, then any user[:token]@ credentials.
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.rsplit("@", 1)[-1]
        # scp form host:owner/repo, or host[:port]/owner/repo.
        host, sep, path = text.partition("/")
        if ":" in host:
            host, _, rest = host.partition(":")
            if not rest.isdigit():
                path = rest + sep + path
        path = path[:-4] if path.endswith(".git") else path
        parts = [p for p in path.split("/") if p]
        if not host or len(parts) < 2:
            return ""
        return "/".join([host] + parts).lower()

    def _resolve_core_root(self) -> Optional[str]:
        """The shared lm core this spoke imports: install_kvm.sh clones it to
        ``<install dir>/lm-core`` (on PYTHONPATH), not the base class's
        /opt/lm, so self-update and rollback must use that checkout."""
        core = os.path.join(self._repo_root(), "lm-core")
        return core if os.path.isdir(os.path.join(core, ".git")) else super()._resolve_core_root()

    def _perform_self_update_sync(self, repo_url: str, core_repo_url: Optional[str] = None,
                                  core_branch: Optional[str] = None,
                                  reason: str = "update") -> Dict[str, Any]:
        """Refuse an update that would repoint this checkout at another repository.

        The hub picks the update source by module_type, and maps every
        "hypervisor" to the pxmx repo (lm core/src/update_pipeline.py
        _UPDATE_SOURCE_MODULE_KEY). The base updater runs
        ``git remote set-url origin <repo_url>`` and pulls, which would replace
        this kvm checkout with pxmx code. So the update runs only when the pushed
        URL names the same repository (host, owner and name) as the current
        origin; an unreadable origin is refused too (fail closed). Credentials
        are stripped before anything is logged, as a URL may carry a token.
        """
        # Read the current origin of this checkout.
        try:
            origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=self._repo_root(),
                                    capture_output=True, text=True, timeout=15).stdout
        except Exception:
            origin = ""
        want, have = self._repo_identity(repo_url), self._repo_identity(origin)
        # A different (or unknown) repository: refuse without touching git.
        if not have or want != have:
            logger.warning("SPOKE_UPDATE refused: pushed repo %r is not this checkout's repo %r",
                           want or "?", have or "unknown")
            return {"status": "ERROR",
                    "message": f"update refused: repo {want or '?'} is not this checkout's repo "
                               f"({have or 'unknown'}); set the kvm update source on the hub"}
        return super()._perform_self_update_sync(repo_url, core_repo_url=core_repo_url,
                                                 core_branch=core_branch, reason=reason)

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
        # Pending command futures keyed by correlation id, with the agent and
        # the exact connection each command was sent on.
        self.pending_responses: Dict[str, Tuple[str, Any, asyncio.Future]] = {}
        self.connected_agents: Dict[str, Dict[str, Any]] = {}
        # Background closes of superseded agent sockets (kept so they aren't GC'd).
        self._close_tasks: set = set()

    # Hypervisor list commands the base control plane would answer with its
    # Proxmox aggregator (pvesh over RUN_COMMAND). KVM agents speak libvirt, so
    # these must reach the kvm module instead.
    _MODULE_LIST_COMMANDS = frozenset(
        {"GET_NODE_STATS", "PXMX_LIST_VMS", "GET_VM_LIST", "AGENT_GET_VM_LIST"})

    async def handle_system_command(self, cmd_type: str, data: Dict[str, Any]) -> Any:
        """Hand the hypervisor list commands to the kvm module; defer the rest to the base."""
        # None tells the base dispatcher to route the command to the module.
        if cmd_type in self._MODULE_LIST_COMMANDS:
            return None
        return await super().handle_system_command(cmd_type, data)

    # ── Agent WebSocket server ────────────────────────────────────────────────

    def _agent_tls_context(self) -> Optional[ssl.SSLContext]:
        """Build the agent listener's TLS context from config, or None when it can't.

        Agents send the shared agent_secret in their first frame, so the
        listener must never accept plaintext. The certificate and key paths
        come from ``agent_tls_cert`` / ``agent_tls_key`` in the KVM config.
        """
        # Both paths are required; a missing one means no TLS.
        cert = self.config.get("agent_tls_cert")
        key = self.config.get("agent_tls_key")
        if not cert or not key:
            return None
        # A bad or unreadable pair is logged and treated as no TLS.
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            return ctx
        except (OSError, ssl.SSLError) as e:
            logger.error(f"KVM agent TLS cert/key could not be loaded: {e}")
            return None

    async def run_agent_server(self):
        """Listen for KVM agent connections on AGENT_PORT over TLS forever.

        Fails closed: with no usable TLS certificate the listener is not
        started at all, because a plaintext listener would expose the shared
        agent secret to anyone on the network.
        """
        # No TLS context: refuse to listen rather than serve plaintext.
        ssl_ctx = self._agent_tls_context()
        if ssl_ctx is None:
            logger.error("KVM agent listener NOT started: set agent_tls_cert and "
                         "agent_tls_key in /etc/lm-kvm/config.json (fail-closed)")
            return
        # Serve agents on all interfaces over TLS; each connection is handled by _agent_handler.
        async with websockets.serve(self._agent_handler, "0.0.0.0", self.AGENT_PORT, ssl=ssl_ctx,
                                    ping_interval=self.AGENT_PING_INTERVAL,
                                    ping_timeout=self.AGENT_PING_TIMEOUT):
            logger.info(f"KVM agent listener on :{self.AGENT_PORT} (TLS)")
            # Await a future that never completes so the server runs until cancelled.
            await asyncio.Future()

    def _owns_record(self, agent_id: str, websocket) -> bool:
        """True when the agent's current record belongs to this connection.

        After a reconnect the old connection must not refresh, overwrite or
        delete the newer connection's record.
        """
        rec = self.connected_agents.get(agent_id)
        return rec is not None and rec.get("ws") is websocket

    @staticmethod
    def _is_open(ws) -> bool:
        """True while a socket's WebSocket state is OPEN (websockets' State enum).

        A socket that is closing or closed (or an object with no state) is
        not open, so its agent_id may be taken by a new connection."""
        return getattr(getattr(ws, "state", None), "name", None) == "OPEN"

    @staticmethod
    async def _close_replaced(ws) -> None:
        """Best-effort close of a connection superseded by a reconnect."""
        try:
            await ws.close(1000, "Replaced by a newer connection")
        except Exception:  # noqa: BLE001 — the old socket may already be gone
            pass

    async def _agent_handler(self, websocket, path=None):
        """Authenticate one agent connection, then process its messages until it closes."""
        # Unknown until the agent introduces itself.
        agent_id = None
        try:
            # The first message must carry the agent id and shared secret, and
            # must arrive promptly so idle unauthenticated sockets can't pile up.
            auth = json.loads(await asyncio.wait_for(websocket.recv(),
                                                     timeout=self.AGENT_AUTH_TIMEOUT))
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

            # Refuse an agent_id whose current connection is still open. Every
            # agent shares one agent_secret and names itself, so without this
            # any agent holding the secret could claim a live host's id, take
            # its commands and answer them. A dead connection is closed by the
            # ping timeout (AGENT_PING_*), which frees the id for the real
            # agent's reconnect. No await between this check and the
            # registration below, so two handshakes can't both pass it.
            old = self.connected_agents.get(agent_id)
            if old and old.get("ws") is not websocket and self._is_open(old.get("ws")):
                logger.warning(f"KVM agent '{agent_id}' refused: that agent_id is already "
                               f"connected from {getattr(old.get('ws'), 'remote_address', None)} "
                               f"(new peer {getattr(websocket, 'remote_address', None)})")
                await websocket.close(1008, "agent_id in use"); return

            # Register the agent with empty telemetry until its first report arrives.
            # A record left by a closing connection is replaced; close that old
            # socket so its handler ends instead of lingering as a second session.
            logger.info(f"KVM agent '{agent_id}' connected")
            self.connected_agents[agent_id] = {
                "ws":           websocket,
                "hostname":     agent_id,
                "last_seen":    time.time(),
                "nodes":        [],
                "vms":          [],
                "agent_metrics": {},
            }
            if old and old.get("ws") is not websocket:
                # Close in the background: a half-open old peer can hold close()
                # for websockets' close_timeout, and the new agent must not wait.
                task = asyncio.create_task(self._close_replaced(old["ws"]))
                self._close_tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)

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
                    if self._owns_record(agent_id, websocket):
                        self.connected_agents[agent_id]["last_seen"] = time.time()

                # Telemetry: refresh last-seen and store the host's nodes, VMs and metrics.
                elif msg_type == "AGENT_TELEMETRY":
                    if self._owns_record(agent_id, websocket):
                        rec = self.connected_agents[agent_id]
                        rec["last_seen"]    = time.time()
                        rec["hostname"]     = data.get("hostname", agent_id)
                        rec["nodes"]        = data.get("nodes", {}).get("nodes", [])
                        rec["vms"]          = data.get("vms",   {}).get("vms",   [])
                        rec["agent_metrics"] = data.get("metrics", {})

                # Response to a command: resolve the waiting future with its data,
                # but only when this agent is the one the command was sent to, so
                # one agent can't answer (or forge the result of) another's command.
                elif msg_type == "AGENT_RESPONSE":
                    pending = self.pending_responses.get(corr_id)
                    if pending is None:
                        continue
                    target, target_ws, fut = pending
                    if target != agent_id or target_ws is not websocket:
                        logger.warning(f"Agent '{agent_id}' answered a command sent to "
                                       f"'{target}' on another connection — dropping")
                        continue
                    del self.pending_responses[corr_id]
                    if not fut.done():
                        fut.set_result(data)

                # Log line: relay it up to the hub in the AGENT_RELAY_UP shape the
                # hub reads (original_payload.payload), without the agent's signature.
                # send_to_hub(type, data) is best-effort and never raises.
                elif msg_type == "AGENT_LOG":
                    # A superseded connection's lines are not relayed as this agent's.
                    if not self._owns_record(agent_id, websocket):
                        continue
                    await self.send_to_hub("AGENT_RELAY_UP", {
                        "agent_id": agent_id,
                        "hostname": self.connected_agents.get(agent_id, {}).get("hostname", agent_id),
                        "original_payload": {"payload": {"type": "AGENT_LOG", "data": data}},
                    })

        # A silent socket (a scanner, a stalled client) just times out: warn
        # without a traceback so port scans don't flood the log.
        except asyncio.TimeoutError:
            logger.warning(f"KVM agent connection timed out during authentication "
                           f"(agent '{agent_id or 'unknown'}')")
        # Any other error ends this connection; log it with the traceback.
        except Exception as e:
            logger.error(f"KVM agent handler error: {e}", exc_info=True)
        # Forget the agent once its connection ends, but only if the record is
        # still this connection's: a reconnect may already have replaced it, and
        # deleting that would drop the live session.
        finally:
            if agent_id and self._owns_record(agent_id, websocket):
                del self.connected_agents[agent_id]
            # Fail this connection's outstanding commands now rather than at
            # their timeout; no other connection may answer them.
            for cid, (_, pws, pfut) in list(self.pending_responses.items()):
                if pws is websocket:
                    del self.pending_responses[cid]
                    if not pfut.done():
                        pfut.set_result({"status": "ERROR", "message": "Agent disconnected"})
            logger.info(f"KVM agent '{agent_id}' disconnected")

    # ── Agent command routing ─────────────────────────────────────────────────

    async def send_to_agent(self, cmd_type: str, data: Dict[str, Any],
                            agent_id: Optional[str] = None,
                            timeout: float = 15.0) -> Dict[str, Any]:
        """Send a command to one connected agent and await its reply.

        An agent must be named: with several hypervisors, picking "the first
        one" could run a command on the wrong host.

        Returns:
            The agent's response data, or an ERROR dict on missing agent/timeout/failure.
        """
        # The named agent must be connected.
        if not agent_id:
            return {"status": "ERROR", "message": "No KVM agent specified"}
        rec = self.connected_agents.get(agent_id)
        if not rec:
            return {"status": "ERROR", "message": f"Agent '{agent_id}' not connected"}
        ws = rec["ws"]

        # Build the command message with a fresh correlation id and sign it.
        corr_id = str(uuid.uuid4())
        msg = {
            "header": {"message_id": corr_id, "timestamp": time.time(),
                       "sender_id": self.spoke_id, "destination_id": agent_id},
            "payload": {"type": cmd_type, "data": data},
        }
        msg["signature"] = self.agent_signer.sign(msg)
        # Register a future that _agent_handler resolves when this agent replies.
        fut = asyncio.get_running_loop().create_future()
        self.pending_responses[corr_id] = (agent_id, ws, fut)
        # Send the command and wait for the reply.
        try:
            await ws.send(json.dumps(msg, separators=(',', ':')))
            return await asyncio.wait_for(fut, timeout=timeout)
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
        # Snapshot the agent ids so replies pair with the agents actually asked,
        # even if one connects or disconnects during the gather.
        agent_ids = list(self.connected_agents)
        if not agent_ids:
            return []
        # Run all sends concurrently; exceptions are returned instead of raised.
        results = await asyncio.gather(
            *[self.send_to_agent(cmd_type, data, aid) for aid in agent_ids],
            return_exceptions=True,
        )
        # Pair each reply with its agent id, turning exceptions into ERROR entries.
        return [
            {"agent_id": aid, **(res if not isinstance(res, Exception) else {"status": "ERROR", "message": str(res)})}
            for aid, res in zip(agent_ids, results)
        ]

    # ── Spoke startup ─────────────────────────────────────────────────────────

    async def run(self):
        """Start the agent listener, register the KVM module and run the hub link."""
        # Announce the hub target and start the agent server in the background.
        logger.info(f"Starting KVM spoke → {self.hub_url}")
        # Keep a reference so the listener task isn't garbage-collected mid-run.
        self._agent_server_task = asyncio.create_task(self.run_agent_server())

        # Imported here to avoid a circular import; the spoke needs this control plane.
        from kvm_spoke import KVMSpoke
        kvm_spoke = KVMSpoke(self.spoke_id, {}, control_plane=self)
        self.register_module("kvm", kvm_spoke)

        # Hand over to the base class connection/heartbeat/dispatch loop.
        await super().run()


if __name__ == "__main__":
    # Command-line flags: spoke identity, secrets and the (required) hub URL.
    parser = argparse.ArgumentParser()
    # Secrets default to the environment (the unit's EnvironmentFile) so they
    # never appear on the command line, where any local user can read them.
    parser.add_argument("--id",         required=True)
    parser.add_argument("--secret",     default=os.environ.get("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", default=os.environ.get("HUB_SECRET", ""))
    parser.add_argument("--hub",        required=True)
    # Read the flags the user passed.
    args = parser.parse_args()

    # Build the control plane and run it until the process is stopped.
    cp = KVMControlPlane(args.id, args.secret, args.hub_secret, args.hub)
    asyncio.run(cp.run())
