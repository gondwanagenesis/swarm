"""mDNS SRV/A record surface + the browse loop that finally listens.

Socket-dependent tests skip (never fail) where multicast is unavailable —
CI containers on ubuntu/windows/macos routinely have no multicast route.
"""

import struct
import threading
import time

import pytest

from swarm.agent import discovery_loop, mdns
from swarm.agent.discovery_loop import DiscoveryLoop, PeerTable
from swarm.agent.mdns import ServiceInstance

HUB_INSTANCE = "hub-deadbeefcafe._swarm._tcp.local"
HUB_HOST = "swarm-hub-deadbeefca.local"


def _requires_multicast():
    if not discovery_loop.multicast_available():
        pytest.skip("no usable multicast socket in this sandbox")


# ---------------------------------------------------------------------------
# record build -> parse
# ---------------------------------------------------------------------------


def test_srv_record_roundtrip():
    header = struct.pack(">6H", 0, 0x8400, 0, 1, 0, 0)
    packet = header + mdns.build_record_srv(HUB_INSTANCE, HUB_HOST, 8777)
    found = mdns.parse_records(packet)
    assert len(found) == 1
    assert found[0].instance == HUB_INSTANCE
    assert found[0].host == HUB_HOST
    assert found[0].port == 8777


def test_a_record_roundtrip():
    header = struct.pack(">6H", 0, 0x8400, 0, 2, 0, 0)
    packet = (
        header
        + mdns.build_record_srv(HUB_INSTANCE, HUB_HOST, 9001)
        + mdns.build_record_a(HUB_HOST, "192.168.1.42")
    )
    found = mdns.parse_records(packet)
    assert found[0].address == "192.168.1.42"
    assert found[0].port == 9001


def test_combined_ptr_srv_a_resolves_to_host_and_port():
    packet = mdns.build_response_service(
        HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7"
    )
    found = mdns.parse_records(packet)
    assert len(found) == 1
    inst = found[0]
    assert inst.instance == HUB_INSTANCE
    assert inst.service == mdns.SERVICE_TYPE
    assert inst.host == HUB_HOST
    assert inst.port == 8777
    assert inst.address == "10.0.0.7"
    assert inst.resolved
    assert inst.is_hub
    assert inst.base_url() == "http://10.0.0.7:8777"


def test_combined_packet_without_address_still_resolves_by_host():
    packet = mdns.build_response_service(HUB_INSTANCE, host=HUB_HOST, port=8777)
    inst = mdns.parse_records(packet)[0]
    assert inst.address is None
    assert inst.base_url() == "http://{}:8777".format(HUB_HOST)


def test_seed_instance_is_not_a_hub():
    packet = mdns.build_response_service(
        "seed-abc123._swarm._tcp.local", host="n.local", port=8788, address="10.0.0.9"
    )
    assert mdns.parse_records(packet)[0].is_hub is False


def test_announce_with_host_and_port_builds_service_packet(monkeypatch):
    """announce() must stay PTR-only without host/port, and go full with them."""
    sent = []

    class FakeSock:
        def setsockopt(self, *a):
            pass

        def sendto(self, data, addr):
            sent.append(data)

        def close(self):
            pass

    monkeypatch.setattr(mdns.socket, "socket", lambda *a, **k: FakeSock())
    mdns.announce(HUB_INSTANCE)
    mdns.announce(HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7")
    assert sent[0] == mdns.build_response_ptr(HUB_INSTANCE)
    assert mdns.parse_records(sent[1])[0].port == 8777


# ---------------------------------------------------------------------------
# never raises
# ---------------------------------------------------------------------------


def test_parse_records_truncated_returns_partial_and_does_not_raise():
    packet = mdns.build_response_service(
        HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7"
    )
    # Chop the A record off; the header still claims three answers.
    truncated = packet[: len(packet) - 12]
    found = mdns.parse_records(truncated)
    assert found, "partial packet should still yield the PTR/SRV it contained"
    assert found[0].instance == HUB_INSTANCE


def test_parse_records_on_garbage_never_raises():
    for junk in (b"", b"\x00", b"\xff" * 40, b"\xc0\xc0" * 20, bytes(range(64))):
        assert isinstance(mdns.parse_records(junk), list)


def test_parse_records_survives_byte_by_byte_truncation():
    packet = mdns.build_response_service(
        HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7"
    )
    for cut in range(len(packet)):
        assert isinstance(mdns.parse_records(packet[:cut]), list)


def test_legacy_parse_response_signature_unchanged():
    """tests/test_recon.py depends on this exact shape."""
    resp = mdns.build_response_ptr("seed-deadbeefcafe._swarm._tcp.local")
    assert mdns.parse_response(resp) == [
        ("_swarm._tcp.local", "seed-deadbeefcafe._swarm._tcp.local")
    ]


def test_primary_ip_never_returns_loopback():
    ip = mdns.primary_ip()
    assert ip is None or not ip.startswith("127.")


# ---------------------------------------------------------------------------
# peer table
# ---------------------------------------------------------------------------


def test_peer_table_ttl_expiry_drops_stale_peer():
    table = PeerTable(default_ttl=120.0)
    now = 1000.0
    table.observe(
        ServiceInstance(instance=HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7", ttl=10),
        now=now,
    )
    assert len(table.peers(now=now + 5)) == 1
    dropped = table.prune(now=now + 11)
    assert dropped == [HUB_INSTANCE]
    assert table.peers(now=now + 11) == []


def test_peer_table_refresh_keeps_peer_alive():
    table = PeerTable()
    inst = ServiceInstance(instance=HUB_INSTANCE, host=HUB_HOST, port=8777, ttl=10)
    table.observe(inst, now=1000.0)
    table.observe(inst, now=1008.0)
    assert len(table.peers(now=1015.0)) == 1
    assert table.peers(now=1020.0) == []


def test_peer_table_thinner_record_does_not_blank_known_fields():
    table = PeerTable()
    table.observe(
        ServiceInstance(instance=HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7"),
        now=1.0,
    )
    table.observe(ServiceInstance(instance=HUB_INSTANCE), now=2.0)
    peer = table.peers(now=2.0)[0]
    assert peer.port == 8777
    assert peer.address == "10.0.0.7"


def test_peer_table_hubs_filters_and_needs_a_url():
    table = PeerTable()
    table.observe(ServiceInstance(instance=HUB_INSTANCE, host=HUB_HOST, port=8777), now=1.0)
    table.observe(
        ServiceInstance(instance="seed-x._swarm._tcp.local", host="s.local", port=8788), now=1.0
    )
    table.observe(ServiceInstance(instance="hub-noport._swarm._tcp.local"), now=1.0)
    hubs = table.hubs(now=1.0)
    assert [h.instance for h in hubs] == [HUB_INSTANCE]


def test_peer_source_ip_beats_unresolvable_host():
    table = PeerTable()
    table.observe(ServiceInstance(instance=HUB_INSTANCE, port=8777), source_ip="10.1.2.3", now=1.0)
    assert table.peers(now=1.0)[0].base_url() == "http://10.1.2.3:8777"


# ---------------------------------------------------------------------------
# socket-dependent
# ---------------------------------------------------------------------------


def test_open_listener_reports_reasons_not_exceptions():
    sock, notes = discovery_loop.open_listener()
    try:
        assert isinstance(notes, list)
        for note in notes:
            assert note.source == discovery_loop.SOURCE
            assert note.message  # every degradation is named
        if sock is None:
            pytest.skip("no usable multicast socket in this sandbox")
    finally:
        if sock is not None:
            sock.close()


def test_discover_hub_returns_none_when_nothing_answers():
    _requires_multicast()
    notes = []
    started = time.time()
    result = discovery_loop.discover_hub(timeout_s=0.6, anomalies=notes)
    elapsed = time.time() - started
    assert result is None or result.startswith("http://")
    assert elapsed < 6.0, "discover_hub must be bounded by its timeout"
    assert isinstance(notes, list)


def test_discover_peers_never_raises_with_zero_timeout():
    assert discovery_loop.discover_peers(timeout_s=0.0) == []


def test_discovery_loop_start_stop_is_clean():
    loop = DiscoveryLoop(query_interval=0.2)
    thread = loop.start()
    time.sleep(0.3)
    started = time.time()
    loop.stop(join_timeout=5.0)
    assert time.time() - started < 5.0, "stop() must not block on a sleep"
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "browse thread did not exit"
    assert threading.active_count() >= 1


def test_discovery_loop_survives_a_dead_socket(monkeypatch):
    """Multicast blocked entirely: no crash, no hang, and a recorded reason."""
    monkeypatch.setattr(
        discovery_loop,
        "open_listener",
        lambda *a, **k: (None, [discovery_loop._anomaly("simulated: multicast blocked")]),
    )
    loop = DiscoveryLoop(query_interval=0.1)
    thread = loop.start()
    time.sleep(0.2)
    status = loop.status()
    assert status.listening is False
    assert any("blocked" in a.message or "disabled" in a.message for a in status.anomalies)
    assert loop.hub_url() is None
    assert loop.peers() == []
    loop.stop(join_timeout=3.0)
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_discovery_loop_ingests_a_packet_without_a_network(monkeypatch):
    """Feed the table directly: PTR+SRV+A becomes a resolvable hub peer."""
    loop = DiscoveryLoop()
    packet = mdns.build_response_service(
        HUB_INSTANCE, host=HUB_HOST, port=8777, address="10.0.0.7"
    )
    for found in mdns.parse_records(packet):
        loop.table.observe(found, source_ip="10.0.0.7")
    assert loop.hub_url() == "http://10.0.0.7:8777"
    assert loop.status().peer_count == 1


def test_discovery_loop_context_manager_stops():
    with DiscoveryLoop(query_interval=0.2) as loop:
        thread = loop._thread
        assert thread is not None
    thread.join(timeout=3.0)
    assert not thread.is_alive()


def test_compression_pointer_loop_does_not_hang():
    """A self-referential pointer must terminate, not spin the browse thread."""
    header = struct.pack(">6H", 0, 0x8400, 0, 1, 0, 0)
    # Answer name is a pointer to itself at offset 12.
    packet = header + b"\xc0\x0c" + struct.pack(">HHIH", mdns.TYPEREC_PTR, 1, 120, 0)
    started = time.time()
    assert mdns.parse_records(packet) == []
    assert mdns.parse_response(packet) == []
    assert time.time() - started < 2.0, "compression loop was not bounded"


def test_valid_compression_pointer_still_decodes():
    """The hop bound must not break legitimate pointer following."""
    header = struct.pack(">6H", 0, 0x8400, 0, 1, 0, 0)
    service = mdns._encode_name(mdns.SERVICE_TYPE)
    # rdata = "hub-x" label + pointer back to the service name at offset 12.
    rdata = b"\x05hub-x" + b"\xc0\x0c"
    answer = service + struct.pack(">HHIH", mdns.TYPEREC_PTR, 1, 120, len(rdata)) + rdata
    found = mdns.parse_records(header + answer)
    assert found and found[0].instance == "hub-x." + mdns.SERVICE_TYPE
