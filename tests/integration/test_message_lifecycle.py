from pathlib import Path
import time

import pytest

from glsim.engine import SimEngine
from glsim.state import StateStore


ROOT = Path(__file__).resolve().parents[2]
RAIL = ROOT / "contracts" / "message_rail.py"
WORKER = ROOT / "examples" / "idempotent_worker.py"
LOST_ACK_WORKER = ROOT / "examples" / "lost_ack_test_worker.py"
ADVERSARIAL_WORKER = ROOT / "examples" / "adversarial_ack_worker.py"
DELIVERY_CAP_WORKER = ROOT / "examples" / "delivery_cap_test_worker.py"
OWNER = "0x1111111111111111111111111111111111111111"
RECIPIENT = "0x2222222222222222222222222222222222222222"
PAYLOAD_HASH = "0x" + "a" * 64


def _deploy_pair(engine, outcome="succeeded"):
    rail_address, rail = engine.deploy(str(RAIL), sender=OWNER)
    worker_address, worker = engine.deploy(
        str(WORKER), [rail_address, outcome], sender=OWNER
    )
    return rail_address, rail, worker_address, worker


def test_request_runs_as_two_finalized_messages_and_persists_recipient_receipt():
    engine = SimEngine(StateStore(seed="message-rail-roundtrip"))
    engine.activate()
    try:
        rail_address, rail, worker_address, worker = _deploy_pair(engine)

        engine.call_method(
            rail_address,
            "create_request",
            ["invoice-42", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )

        # The worker child transaction has run, but its acknowledgement is a
        # second asynchronous message and has not yet updated the coordinator.
        assert worker.processed_effects == 1
        before_ack = engine.call_method(
            rail_address, "get_request", ["invoice-42"], sender=OWNER
        )
        assert before_ack["state"] == "awaiting_ack"

        after_ack = engine.call_method(
            rail_address, "get_request", ["invoice-42"], sender=OWNER
        )
        receipt = engine.call_method(
            worker_address, "get_result", ["invoice-42"], sender=OWNER
        )
        assert after_ack["state"] == "succeeded"
        assert after_ack["result_hash"] == receipt["result_hash"]
        assert receipt["processed_effects"] == 1
    finally:
        engine.deactivate()


def test_retry_recovers_a_lost_ack_without_repeating_the_worker_effect():
    engine = SimEngine(StateStore(seed="message-rail-idempotent-retry"))
    engine.activate()
    try:
        rail_address, rail, worker_address, worker = _deploy_pair(engine)
        now = {"value": int(time.time())}
        rail._now = lambda: now["value"]

        engine.call_method(
            rail_address,
            "create_request",
            ["invoice-43", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        assert worker.processed_effects == 1

        # Simulate the async acknowledgement not being delivered. The worker
        # has committed its idempotency record; a retry must only replay it.
        engine._post_queue.clear()
        now["value"] += 30
        engine.call_method(
            rail_address, "retry_request", ["invoice-43"], sender=OWNER
        )
        assert worker.processed_effects == 1
        assert rail.get_request("invoice-43")["attempts"] == 2

        engine.call_method(rail_address, "get_request", ["invoice-43"], sender=OWNER)
        final = engine.call_method(
            rail_address, "get_request", ["invoice-43"], sender=OWNER
        )
        receipt = engine.call_method(
            worker_address, "get_result", ["invoice-43"], sender=OWNER
        )
        assert final["state"] == "succeeded"
        assert final["result_hash"] == receipt["result_hash"]
        assert receipt["processed_effects"] == 1

    finally:
        engine.deactivate()


def test_live_test_worker_withholds_first_ack_then_replays_saved_result_once():
    engine = SimEngine(StateStore(seed="message-rail-live-lost-ack-worker"))
    engine.activate()
    try:
        rail_address, rail = engine.deploy(str(RAIL), sender=OWNER)
        worker_address, worker = engine.deploy(
            str(LOST_ACK_WORKER), [rail_address, "succeeded"], sender=OWNER
        )
        now = {"value": int(time.time())}
        rail._now = lambda: now["value"]

        engine.call_method(
            rail_address,
            "create_request",
            ["invoice-live-lost-ack", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        assert worker.processed_effects == 1
        first = engine.call_method(
            rail_address, "get_request", ["invoice-live-lost-ack"], sender=OWNER
        )
        assert first["state"] == "awaiting_ack"

        now["value"] += 30
        engine.call_method(
            rail_address, "retry_request", ["invoice-live-lost-ack"], sender=OWNER
        )
        assert worker.processed_effects == 1
        assert rail.get_request("invoice-live-lost-ack")["attempts"] == 2

        engine.call_method(
            rail_address, "get_request", ["invoice-live-lost-ack"], sender=OWNER
        )
        final = engine.call_method(
            rail_address, "get_request", ["invoice-live-lost-ack"], sender=OWNER
        )
        receipt = engine.call_method(
            worker_address, "get_result", ["invoice-live-lost-ack"], sender=OWNER
        )
        assert final["state"] == "succeeded"
        assert final["result_hash"] == receipt["result_hash"]
        assert receipt["processed_effects"] == 1

        # The next unique request must also withhold its first ack. Suppression
        # is per request, not a one-time flag for the entire worker contract.
        second_request_id = "invoice-live-lost-ack-second"
        engine.call_method(
            rail_address,
            "create_request",
            [second_request_id, worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        assert worker.processed_effects == 2
        second_first = engine.call_method(
            rail_address, "get_request", [second_request_id], sender=OWNER
        )
        assert second_first["state"] == "awaiting_ack"

        now["value"] += 30
        engine.call_method(
            rail_address, "retry_request", [second_request_id], sender=OWNER
        )
        assert worker.processed_effects == 2
        assert rail.get_request(second_request_id)["attempts"] == 2

        engine.call_method(
            rail_address, "get_request", [second_request_id], sender=OWNER
        )
        second_final = engine.call_method(
            rail_address, "get_request", [second_request_id], sender=OWNER
        )
        second_receipt = engine.call_method(
            worker_address, "get_result", [second_request_id], sender=OWNER
        )
        assert second_final["state"] == "succeeded"
        assert second_final["result_hash"] == second_receipt["result_hash"]
        assert second_receipt["processed_effects"] == 2
    finally:
        engine.deactivate()


def test_worker_rejects_direct_spoof_and_coordinator_records_a_failed_receipt():
    engine = SimEngine(StateStore(seed="message-rail-failed-receipt"))
    engine.activate()
    try:
        rail_address, _rail, worker_address, worker = _deploy_pair(engine, "failed")
        with pytest.raises(Exception, match="Only the configured coordinator may dispatch requests"):
            engine.call_method(
                worker_address,
                "handle_request",
                [rail_address, "forged-1", 1, PAYLOAD_HASH, int(time.time()) + 600],
                sender=RECIPIENT,
            )

        engine.call_method(
            rail_address,
            "create_request",
            ["invoice-44", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        engine.call_method(rail_address, "get_request", ["invoice-44"], sender=OWNER)
        result = engine.call_method(
            rail_address, "get_request", ["invoice-44"], sender=OWNER
        )
        assert result["state"] == "failed"
        assert result["result_code"] == "failed"
        assert worker.processed_effects == 1
    finally:
        engine.deactivate()


def _deploy_adversarial_pair(engine, mode):
    rail_address, rail = engine.deploy(str(RAIL), sender=OWNER)
    worker_address, worker = engine.deploy(
        str(ADVERSARIAL_WORKER), [rail_address, mode], sender=OWNER
    )
    return rail_address, rail, worker_address, worker


def test_coordinator_rejects_wrong_caller_and_leaves_request_pending():
    engine = SimEngine(StateStore(seed="message-rail-wrong-caller"))
    engine.activate()
    try:
        rail_address, rail, worker_address, _worker = _deploy_adversarial_pair(
            engine, "hold"
        )
        engine.call_method(
            rail_address,
            "create_request",
            ["wrong-caller-live", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )

        with pytest.raises(Exception, match="Only the registered recipient may acknowledge"):
            engine.call_method(
                rail_address,
                "acknowledge",
                ["wrong-caller-live", 1, "succeeded", "0x" + "b" * 64],
                sender=RECIPIENT,
            )

        request = engine.call_method(
            rail_address, "get_request", ["wrong-caller-live"], sender=OWNER
        )
        assert request["state"] == "awaiting_ack"
        assert request["result_code"] == ""
    finally:
        engine.deactivate()


def test_wrong_sequence_callback_cannot_complete_request():
    engine = SimEngine(StateStore(seed="message-rail-wrong-sequence"))
    engine.activate()
    try:
        rail_address, _rail, worker_address, worker = _deploy_adversarial_pair(
            engine, "wrong_sequence"
        )
        engine.call_method(
            rail_address,
            "create_request",
            ["wrong-sequence-live", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )

        engine.call_method(
            rail_address, "get_request", ["wrong-sequence-live"], sender=OWNER
        )
        request = engine.call_method(
            rail_address, "get_request", ["wrong-sequence-live"], sender=OWNER
        )
        assert worker.processed_effects == 1
        assert request["state"] == "awaiting_ack"
        assert request["result_code"] == ""
    finally:
        engine.deactivate()


def test_conflicting_duplicate_ack_cannot_rewrite_terminal_result():
    engine = SimEngine(StateStore(seed="message-rail-conflicting-ack"))
    engine.activate()
    try:
        rail_address, _rail, worker_address, worker = _deploy_adversarial_pair(
            engine, "succeed"
        )
        engine.call_method(
            rail_address,
            "create_request",
            ["conflicting-ack-live", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        engine.call_method(
            rail_address, "get_request", ["conflicting-ack-live"], sender=OWNER
        )
        original = engine.call_method(
            rail_address, "get_request", ["conflicting-ack-live"], sender=OWNER
        )
        assert original["state"] == "succeeded"

        engine.call_method(
            worker_address,
            "send_conflicting_ack",
            ["conflicting-ack-live"],
            sender=OWNER,
        )
        engine.call_method(
            rail_address, "get_request", ["conflicting-ack-live"], sender=OWNER
        )
        final = engine.call_method(
            rail_address, "get_request", ["conflicting-ack-live"], sender=OWNER
        )
        assert worker.processed_effects == 1
        assert final["state"] == "succeeded"
        assert final["result_code"] == original["result_code"]
        assert final["result_hash"] == original["result_hash"]
    finally:
        engine.deactivate()


def test_late_ack_after_expiry_cannot_revive_request():
    engine = SimEngine(StateStore(seed="message-rail-late-ack"))
    engine.activate()
    try:
        rail_address, rail, worker_address, _worker = _deploy_adversarial_pair(
            engine, "hold"
        )
        now = {"value": int(time.time())}
        rail._now = lambda: now["value"]
        engine.call_method(
            rail_address,
            "create_request",
            ["late-ack-live", worker_address, PAYLOAD_HASH, 60],
            sender=OWNER,
        )

        now["value"] += 60
        engine.call_method(
            rail_address, "expire_request", ["late-ack-live"], sender=RECIPIENT
        )
        engine.call_method(
            worker_address, "send_ack", ["late-ack-live"], sender=OWNER
        )
        engine.call_method(
            rail_address, "get_request", ["late-ack-live"], sender=OWNER
        )
        final = engine.call_method(
            rail_address, "get_request", ["late-ack-live"], sender=OWNER
        )

        assert final["state"] == "expired"
        assert final["result_code"] == ""
        assert final["result_hash"] == ""
    finally:
        engine.deactivate()


def test_silent_idempotent_receiver_reaches_delivery_cap_without_repeating_effect():
    engine = SimEngine(StateStore(seed="message-rail-delivery-cap"))
    engine.activate()
    try:
        rail_address, rail = engine.deploy(str(RAIL), sender=OWNER)
        worker_address, worker = engine.deploy(
            str(DELIVERY_CAP_WORKER), [rail_address], sender=OWNER
        )
        now = {"value": int(time.time())}
        rail._now = lambda: now["value"]

        engine.call_method(
            rail_address,
            "create_request",
            ["delivery-cap-live", worker_address, PAYLOAD_HASH, 600],
            sender=OWNER,
        )
        assert worker.processed_effects == 1

        for expected_attempts in (2, 3):
            now["value"] += 30
            engine.call_method(
                rail_address,
                "retry_request",
                ["delivery-cap-live"],
                sender=OWNER,
            )
            request = engine.call_method(
                rail_address,
                "get_request",
                ["delivery-cap-live"],
                sender=OWNER,
            )
            assert request["state"] == "awaiting_ack"
            assert request["attempts"] == expected_attempts
            assert worker.processed_effects == 1

        now["value"] += 30
        with pytest.raises(Exception, match="Maximum delivery attempts reached"):
            engine.call_method(
                rail_address,
                "retry_request",
                ["delivery-cap-live"],
                sender=OWNER,
            )

        final = engine.call_method(
            rail_address, "get_request", ["delivery-cap-live"], sender=OWNER
        )
        receipt = engine.call_method(
            worker_address, "get_result", ["delivery-cap-live"], sender=OWNER
        )
        assert final["state"] == "awaiting_ack"
        assert final["attempts"] == 3
        assert receipt["processed_effects"] == 1
    finally:
        engine.deactivate()
