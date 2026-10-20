import sys

import pytest


CONTRACT = "contracts/message_rail.py"
PAYLOAD_HASH = "0x" + "a" * 64
RESULT_HASH = "0x" + "b" * 64


class FakeWorker:
    def __init__(self):
        self.calls = []

    def emit(self, on):
        assert on == "finalized"
        return self

    def handle_request(self, coordinator, request_id, sequence, payload_hash, expires_at):
        self.calls.append((coordinator, request_id, sequence, payload_hash, expires_at))


def _deploy(direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch, now=1_000):
    direct_vm.sender = direct_alice
    rail = direct_deploy(CONTRACT, sdk_version="v0.2.16")
    module = sys.modules[rail._instance.__class__.__module__]
    monkeypatch.setattr(rail._instance, "_now", lambda: now)
    worker = FakeWorker()
    monkeypatch.setattr(module.gl, "get_contract_at", lambda _address: worker)
    return rail, worker


def test_request_is_recorded_before_finalized_child_message_is_emitted(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    recipient = "0x" + direct_bob.hex()

    rail.create_request("job-001", recipient, PAYLOAD_HASH, 600)

    request = rail.get_request("job-001")
    assert request["state"] == "awaiting_ack"
    assert request["sequence"] == 1
    assert request["attempts"] == 1
    assert request["payload_hash"] == PAYLOAD_HASH
    assert len(worker.calls) == 1
    assert worker.calls[0][1:4] == ("job-001", 1, PAYLOAD_HASH)


def test_acknowledgement_is_bound_to_recipient_and_exact_request(
    direct_deploy, direct_vm, direct_alice, direct_bob, direct_charlie, monkeypatch
):
    rail, _worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    recipient = "0x" + direct_bob.hex()
    rail.create_request("job-002", recipient, PAYLOAD_HASH, 600)

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("Only the registered recipient may acknowledge"):
        rail.acknowledge("job-002", 1, "succeeded", RESULT_HASH)

    direct_vm.sender = direct_bob
    rail.acknowledge("job-002", 1, "succeeded", RESULT_HASH)
    assert rail.get_request("job-002")["state"] == "succeeded"


def test_exact_acknowledgement_replay_is_idempotent_but_conflict_is_rejected(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, _worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    rail.create_request("job-003", "0x" + direct_bob.hex(), PAYLOAD_HASH, 600)
    direct_vm.sender = direct_bob

    rail.acknowledge("job-003", 1, "succeeded", RESULT_HASH)
    rail.acknowledge("job-003", 1, "succeeded", RESULT_HASH)
    assert rail.get_request("job-003")["state"] == "succeeded"

    with direct_vm.expect_revert("Conflicting acknowledgement for a terminal request"):
        rail.acknowledge("job-003", 1, "failed", "0x" + "c" * 64)


def test_request_id_is_single_use_and_sequence_mismatch_cannot_complete_it(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    rail.create_request("job-003a", "0x" + direct_bob.hex(), PAYLOAD_HASH, 600)

    with direct_vm.expect_revert("Request id has already been used"):
        rail.create_request("job-003a", "0x" + direct_bob.hex(), PAYLOAD_HASH, 600)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Acknowledgement sequence does not match"):
        rail.acknowledge("job-003a", 2, "succeeded", RESULT_HASH)

    assert len(worker.calls) == 1
    assert rail.get_request("job-003a")["state"] == "awaiting_ack"


def test_retry_reuses_the_same_idempotency_key_and_commitment(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    rail.create_request("job-004", "0x" + direct_bob.hex(), PAYLOAD_HASH, 600)
    monkeypatch.setattr(rail._instance, "_now", lambda: 1_030)

    rail.retry_request("job-004")

    assert len(worker.calls) == 2
    assert worker.calls[0][1:4] == worker.calls[1][1:4]
    assert rail.get_request("job-004")["attempts"] == 2


def test_retry_requires_owner_delay_and_stops_at_delivery_limit(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    now = {"value": 1_000}
    monkeypatch.setattr(rail._instance, "_now", lambda: now["value"])
    rail.create_request("job-004a", "0x" + direct_bob.hex(), PAYLOAD_HASH, 600)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the request owner may retry delivery"):
        rail.retry_request("job-004a")

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Retry delay has not elapsed"):
        rail.retry_request("job-004a")
    now["value"] += 30
    rail.retry_request("job-004a")
    now["value"] += 30
    rail.retry_request("job-004a")
    now["value"] += 30
    with direct_vm.expect_revert("Maximum delivery attempts reached"):
        rail.retry_request("job-004a")

    assert len(worker.calls) == 3
    assert rail.get_request("job-004a")["attempts"] == 3


def test_expired_request_rejects_late_ack_and_can_only_be_closed_once(
    direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
):
    rail, _worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )
    rail.create_request("job-005", "0x" + direct_bob.hex(), PAYLOAD_HASH, 60)
    monkeypatch.setattr(rail._instance, "_now", lambda: 1_060)
    direct_vm.sender = direct_bob

    with direct_vm.expect_revert("Request deadline has passed"):
        rail.acknowledge("job-005", 1, "succeeded", RESULT_HASH)

    rail.expire_request("job-005")
    assert rail.get_request("job-005")["state"] == "expired"
    with direct_vm.expect_revert("Request is not awaiting acknowledgement"):
        rail.expire_request("job-005")


@pytest.mark.parametrize(
    ("request_id", "recipient", "payload_hash", "timeout_seconds", "message"),
    [
        ("bad id", "0x" + "1" * 40, PAYLOAD_HASH, 600, "request id contains unsupported characters"),
        ("job-006", "0x1234", PAYLOAD_HASH, 600, "recipient address is invalid"),
        ("job-007", "0x" + "1" * 40, "0x1234", 600, "payload hash must be 32-byte hex"),
        ("job-008", "0x" + "1" * 40, PAYLOAD_HASH, 10, "timeout must be between"),
    ],
)
def test_malformed_request_is_rejected_before_dispatch(
    direct_deploy,
    direct_vm,
    direct_alice,
    direct_bob,
    monkeypatch,
    request_id,
    recipient,
    payload_hash,
    timeout_seconds,
    message,
):
    rail, worker = _deploy(
        direct_deploy, direct_vm, direct_alice, direct_bob, monkeypatch
    )

    with direct_vm.expect_revert(message):
        rail.create_request(request_id, recipient, payload_hash, timeout_seconds)

    assert worker.calls == []
