"""Fresh source evidence, explicit channel presence, and retained observations."""

from datetime import datetime, timedelta, timezone
from unittest import TestCase
from uuid import UUID

from nautobot_ssot_unifi.ssot._neighbors import _evidence, _NeighborHold, _observation, _pair


class NeighborEvidenceTests(TestCase):
    """Verify neighbor evidence behavior."""

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    peer = UUID("00000000-0000-0000-0000-000000000002")

    def device(self, **changes):
        """Build a device fixture for the tested behavior."""
        return {
            "state": 1,
            "last_seen": self.now.timestamp() - 1,
            "next_heartbeat_at": self.now.timestamp() + 10,
            "uplink": {"type": "wireless"},
            **changes,
        }

    def resolve(self, chassis, port, kind):
        """Resolve a synthetic peer against the bound fixture identities."""
        if (chassis, port, kind) != ("peer-mac", "eth0", "ifname"):
            raise _NeighborHold("unresolved-endpoint")
        return self.peer

    def test_explicit_empty_and_one_precise_lldp_report(self):
        self.assertEqual(_evidence(self.device(), {"lldp_table": []}, self.now, None, self.resolve), {})
        port = {"lldp_table": [{"lldp_chassis_id": "peer-mac", "lldp_port_id": "eth0"}]}
        self.assertEqual(_evidence(self.device(), port, self.now, None, self.resolve), {self.peer: {"lldp"}})

    def test_stale_disconnected_missing_and_cached_reports_hold(self):
        cases = [
            self.device(state=0),
            self.device(state=6),
            self.device(last_seen=None),
            self.device(next_heartbeat_at=None),
            self.device(last_seen=self.now.timestamp() + 1),
            self.device(next_heartbeat_at=self.now.timestamp() - 1),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(_NeighborHold):
                _evidence(raw, {"lldp_table": []}, self.now, None, self.resolve)
        for port, raw in [
            ({}, self.device()),
            ({"lldp_table": []}, {k: v for k, v in self.device().items() if k != "uplink"}),
            ({"lldp_table": None}, self.device()),
        ]:
            with self.assertRaises(_NeighborHold):
                _evidence(raw, port, self.now, None, self.resolve)
        with self.assertRaises(_NeighborHold):
            _evidence(self.device(), {"lldp_table": []}, self.now, self.now, self.resolve)

    def test_unresolved_peer_never_becomes_authoritative_omission(self):
        with self.assertRaises(_NeighborHold) as caught:
            _evidence(
                self.device(),
                {"lldp_table": [{"lldp_chassis_id": "unknown", "lldp_port_id": "Port 1"}]},
                self.now,
                None,
                self.resolve,
            )
        self.assertEqual(caught.exception.reason, "unresolved-endpoint")

    def test_omission_retains_last_evidence_and_reappearance_reuses_entry(self):
        observed = _observation(None, "00000000-0000-0000-0000-000000000001", "site-a", {self.peer: {"lldp"}}, self.now)
        absent = _observation(observed, observed["integration_id"], "site-a", {}, self.now + timedelta(seconds=30))
        peer = absent["peers"][0]
        self.assertEqual(peer["presence"], "not-observed")
        self.assertEqual(peer["evidence"], ["lldp"])
        self.assertEqual(peer["last_observed_at"], observed["peers"][0]["last_observed_at"])
        returned = _observation(
            absent,
            observed["integration_id"],
            "site-a",
            {self.peer: {"wired-uplink"}},
            self.now + timedelta(seconds=60),
        )
        self.assertEqual(len(returned["peers"]), 1)
        self.assertEqual(returned["peers"][0]["first_observed_at"], observed["peers"][0]["first_observed_at"])
        self.assertEqual(returned["peers"][0]["presence"], "observed")

    def test_wired_uplink_needs_explicit_applicability_and_remote_port(self):
        device = self.device(uplink={"type": "wire", "uplink_mac": "peer-mac", "uplink_remote_port": 3})
        self.assertEqual(_evidence(device, {"lldp_table": [], "is_uplink": False}, self.now, None, self.resolve), {})
        with self.assertRaises(_NeighborHold):
            _evidence(device, {"lldp_table": []}, self.now, None, self.resolve)
        calls = []

        def resolve(chassis, port, kind):
            calls.append((chassis, port, kind))
            return self.peer

        self.assertEqual(
            _evidence(device, {"lldp_table": [], "is_uplink": True}, self.now, None, resolve),
            {self.peer: {"wired-uplink"}},
        )
        self.assertEqual(calls, [("peer-mac", 3, "port_idx")])

    def test_pair_is_unordered_and_self_links_are_rejected(self):
        other = UUID("00000000-0000-0000-0000-000000000003")
        self.assertEqual(_pair(other, self.peer), _pair(self.peer, other))
        with self.assertRaises(_NeighborHold):
            _pair(other, other)
