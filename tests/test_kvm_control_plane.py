"""Agent-listener hardening tests for KVMControlPlane (lm core stubbed out)."""
import asyncio
import json
import logging
import sys
import types
from pathlib import Path

import pytest


class _BaseControlPlane:
    """Stand-in for lm's BaseControlPlane: records hub sends, answers system commands."""

    def __init__(self, spoke_id, secret, hub_secret=None, hub_url=None):
        self.spoke_id = spoke_id
        self.hub_sent = []

    async def handle_system_command(self, cmd_type, data):
        return {"status": "SUCCESS", "from": "base"}

    async def send_to_hub(self, payload_type, data):
        self.hub_sent.append((payload_type, data))
        return True

    def _repo_root(self):
        return self.repo_root

    def _perform_self_update_sync(self, repo_url, core_repo_url=None, core_branch=None, reason="update"):
        self.updated_from = repo_url
        return {"status": "SUCCESS"}


class _Signer:
    """Stand-in signer: a message is valid when its signature is "ok"."""

    def __init__(self, secret):
        self.secret = secret

    def sign(self, msg):
        return "ok"

    def verify(self, msg):
        return msg.get("signature") == "ok"


def _stub(name):
    """Return the stub module at name, creating it (and registering it) if needed."""
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    return mod


_stub("core"); _stub("core.src")
_stub("core.src.messaging")
_stub("core.src.messaging.control_plane").BaseControlPlane = _BaseControlPlane
_stub("core.src.security")
_stub("core.src.security.signer").MessageSigner = _Signer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import control_plane as kcp  # noqa: E402


class _FakeWS:
    """Agent socket: replies to recv() in order, then yields the post-auth frames."""

    def __init__(self, handshake, frames, on_frame=None):
        self._handshake = list(handshake)
        self._frames = list(frames)
        self._on_frame = on_frame
        self.sent, self.closed = [], None
        self.state_name = "OPEN"

    @property
    def state(self):
        """websockets-style state: OPEN until close() is called."""
        return types.SimpleNamespace(name="CLOSED" if self.closed else self.state_name)

    async def recv(self):
        if not self._handshake:
            await asyncio.sleep(3600)
        return self._handshake.pop(0)

    async def send(self, raw):
        self.sent.append(raw)

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for f in self._frames:
            if self._on_frame:
                self._on_frame()
            yield f


def _cp():
    """A control plane with a known agent secret."""
    cp = kcp.KVMControlPlane("kvm-1", "spoke-secret")
    cp.agent_secret = "agent-secret"
    return cp


def _auth(agent_id):
    """The two handshake frames an agent sends: credentials, then HUB_OK."""
    return [json.dumps({"agent_id": agent_id, "secret": "agent-secret"}),
            json.dumps({"status": "HUB_OK"})]


def _frame(msg_type, data, corr_id=None):
    """A correctly signed agent frame."""
    return json.dumps({"header": {"correlation_id": corr_id},
                       "payload": {"type": msg_type, "data": data}, "signature": "ok"})


@pytest.mark.asyncio
async def test_list_commands_go_to_kvm_module_not_pxmx_aggregator():
    cp = _cp()
    for cmd in ("GET_NODE_STATS", "PXMX_LIST_VMS", "GET_VM_LIST", "AGENT_GET_VM_LIST"):
        assert await cp.handle_system_command(cmd, {}) is None
    assert (await cp.handle_system_command("HUB_PING", {}))["from"] == "base"


@pytest.mark.asyncio
async def test_send_to_agent_needs_a_named_agent_and_honours_timeout():
    cp = _cp()
    ws = _FakeWS([], [])
    cp.connected_agents = {"a1": {"ws": ws}}
    assert (await cp.send_to_agent("GET_VM_INFO", {}))["status"] == "ERROR"
    assert ws.sent == []
    res = await cp.send_to_agent("GET_VM_INFO", {}, agent_id="a1", timeout=0.01)
    assert res == {"status": "ERROR", "message": "Agent response timeout"}
    assert cp.pending_responses == {}


@pytest.mark.asyncio
async def test_agent_cannot_answer_another_agents_command():
    cp = _cp()
    fut = asyncio.get_running_loop().create_future()
    a1_ws = _FakeWS(_auth("a1"), [_frame("AGENT_RESPONSE", {"status": "SUCCESS"}, "c1")])
    cp.pending_responses["c1"] = ("a1", a1_ws, fut)
    ws = _FakeWS(_auth("a2"), [_frame("AGENT_RESPONSE", {"status": "SUCCESS"}, "c1")])
    await cp._agent_handler(ws)
    assert not fut.done()
    assert "c1" in cp.pending_responses
    # The same agent_id on another connection can't answer it either.
    ws = _FakeWS(_auth("a1"), [_frame("AGENT_RESPONSE", {"status": "FORGED"}, "c1")])
    await cp._agent_handler(ws)
    assert not fut.done()
    # The connection the command was sent on resolves it.
    await cp._agent_handler(a1_ws)
    assert fut.result() == {"status": "SUCCESS"}
    assert "c1" not in cp.pending_responses


@pytest.mark.asyncio
async def test_old_connection_closing_keeps_the_reconnected_session():
    cp = _cp()
    newer = {"ws": object(), "hostname": "a1"}

    def _reconnect():
        cp.connected_agents["a1"] = newer

    await cp._agent_handler(_FakeWS(_auth("a1"), [_frame("AGENT_HEARTBEAT", {}),
                                                  _frame("AGENT_TELEMETRY", {"hostname": "spoofed"}),
                                                  _frame("AGENT_LOG", {"message": "spoofed"})],
                                    _reconnect))
    assert cp.connected_agents["a1"] is newer
    # Its log lines are not relayed as the newer session's.
    assert cp.hub_sent == []
    # The superseded connection did not write into the newer record.
    assert newer == {"ws": newer["ws"], "hostname": "a1"}
    # A connection that is still the current one is removed when it closes.
    await cp._agent_handler(_FakeWS(_auth("a2"), []))
    assert "a2" not in cp.connected_agents


@pytest.mark.asyncio
async def test_agent_log_relays_in_the_shape_the_hub_reads():
    cp = _cp()
    await cp._agent_handler(_FakeWS(_auth("a1"), [_frame("AGENT_LOG", {"level": "INFO", "message": "hi"})]))
    assert cp.hub_sent == [("AGENT_RELAY_UP", {
        "agent_id": "a1", "hostname": "a1",
        "original_payload": {"payload": {"type": "AGENT_LOG",
                                         "data": {"level": "INFO", "message": "hi"}}},
    })]


@pytest.mark.asyncio
async def test_silent_unauthenticated_connection_is_dropped(monkeypatch):
    cp = _cp()
    monkeypatch.setattr(cp, "AGENT_AUTH_TIMEOUT", 0.01)
    await asyncio.wait_for(cp._agent_handler(_FakeWS([], [])), timeout=1)
    assert cp.connected_agents == {}


@pytest.mark.asyncio
async def test_listener_fails_closed_without_tls(monkeypatch):
    cp = _cp()

    def _no_serve(*a, **k):
        raise AssertionError("plaintext listener started")

    monkeypatch.setattr(kcp.websockets, "serve", _no_serve)
    cp.config = {}
    assert await cp.run_agent_server() is None
    cp.config = {"agent_tls_cert": "/nonexistent.crt", "agent_tls_key": "/nonexistent.key"}
    assert await cp.run_agent_server() is None


@pytest.mark.asyncio
async def test_reconnect_closes_the_replaced_socket_without_waiting_for_it():
    cp = _cp()
    old_ws = _FakeWS([], [])
    old_ws.state_name = "CLOSING"
    cp.connected_agents["a1"] = {"ws": old_ws, "hostname": "a1"}
    await cp._agent_handler(_FakeWS(_auth("a1"), []))
    await asyncio.gather(*cp._close_tasks)
    assert old_ws.closed is not None

    # A half-open old peer whose close() hangs must not hold up the new agent.
    class _HangingWS(_FakeWS):
        async def close(self, code=None, reason=None):
            await asyncio.sleep(3600)
    seen = []
    hanging = _HangingWS([], [])
    hanging.state_name = "CLOSING"
    cp.connected_agents["a1"] = {"ws": hanging, "hostname": "a1"}
    await asyncio.wait_for(cp._agent_handler(_FakeWS(
        _auth("a1"), [_frame("AGENT_HEARTBEAT", {})], lambda: seen.append(1))), timeout=1)
    assert seen == [1]
    for t in list(cp._close_tasks):
        t.cancel()


@pytest.mark.asyncio
async def test_live_agent_id_cannot_be_taken_over():
    """A second connection claiming a connected agent's id is refused, and the
    live session keeps its record and its pending command."""
    cp = _cp()
    live = _FakeWS([], [])
    cp.connected_agents["a1"] = {"ws": live, "hostname": "a1"}
    fut = asyncio.get_running_loop().create_future()
    cp.pending_responses["c1"] = ("a1", live, fut)
    intruder = _FakeWS(_auth("a1"), [_frame("AGENT_RESPONSE", {"status": "FORGED"}, "c1")])
    await cp._agent_handler(intruder)
    assert intruder.closed == (1008, "agent_id in use")
    assert cp.connected_agents["a1"]["ws"] is live and live.closed is None
    assert not fut.done() and "c1" in cp.pending_responses


@pytest.mark.asyncio
async def test_disconnect_fails_that_connections_pending_commands():
    cp = _cp()
    ws = _FakeWS(_auth("a1"), [])
    fut = asyncio.get_running_loop().create_future()
    cp.pending_responses["c1"] = ("a1", ws, fut)
    await cp._agent_handler(ws)
    assert fut.result() == {"status": "ERROR", "message": "Agent disconnected"}
    assert cp.pending_responses == {}


@pytest.mark.asyncio
async def test_auth_timeout_logs_a_warning_without_traceback(monkeypatch, caplog):
    cp = _cp()
    monkeypatch.setattr(cp, "AGENT_AUTH_TIMEOUT", 0.01)
    caplog.set_level(logging.WARNING)
    await asyncio.wait_for(cp._agent_handler(_FakeWS([], [])), timeout=1)
    timeouts = [r for r in caplog.records if "timed out" in r.getMessage()]
    assert timeouts and all(r.levelno == logging.WARNING and not r.exc_info for r in timeouts)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.parametrize("pushed, runs", [
    ("https://github.com/lbockenstedt/pxmx.git", False),   # the hub's "hypervisor" source
    ("https://x-access-token:t@github.com/lbockenstedt/kvm.git", True),
    ("git@github.com:lbockenstedt/kvm", True),
    ("ssh://git@github.com:22/lbockenstedt/kvm.git", True),
    ("https://github.com/other-owner/kvm.git", False),     # same-named fork
    ("https://evil.example/lbockenstedt/kvm.git", False),  # another host
    ("kvm", False),
])
def test_spoke_update_refuses_another_repo(tmp_path, pushed, runs):
    """The hub maps "hypervisor" to pxmx; that update must not repoint the kvm checkout."""
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/lbockenstedt/kvm.git"], check=True)
    cp = _cp()
    cp.repo_root = str(tmp_path)
    res = cp._perform_self_update_sync(pushed, reason="spoke-update")
    assert (res["status"] == "SUCCESS") is runs
    assert (getattr(cp, "updated_from", None) == pushed) is runs


def test_spoke_update_refused_when_origin_is_unreadable(tmp_path):
    cp = _cp()
    cp.repo_root = str(tmp_path)  # not a git checkout
    res = cp._perform_self_update_sync("https://github.com/lbockenstedt/kvm.git")
    assert res["status"] == "ERROR" and not hasattr(cp, "updated_from")


def test_core_root_is_the_installers_lm_core(tmp_path):
    """install_kvm.sh imports core from <install dir>/lm-core; self-update must use it."""
    (tmp_path / "lm-core" / ".git").mkdir(parents=True)
    cp = _cp()
    cp.repo_root = str(tmp_path)
    assert cp._resolve_core_root() == str(tmp_path / "lm-core")
