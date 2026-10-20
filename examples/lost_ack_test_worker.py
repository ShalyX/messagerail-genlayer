# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


ERROR_EXPECTED = "[EXPECTED]"
OUTCOMES = ("succeeded", "failed")


@allow_storage
@dataclass
class ProcessedRequest:
    sequence: u256
    payload_hash: str
    result_code: str
    result_hash: str


def _expected(message: str) -> None:
    raise gl.vm.UserError(f"{ERROR_EXPECTED} {message}")


def _address_text(value) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    try:
        return value.as_hex.lower()
    except AttributeError:
        return str(value).strip().lower()


class IdempotentWorker(gl.Contract):
    """TEST ONLY receiver for lost-ack and duplicate-delivery verification."""

    coordinator_address: str
    outcome: str
    processed_effects: u256
    requests: TreeMap[str, ProcessedRequest]

    def __init__(self, coordinator_address: str, outcome: str = "succeeded"):
        normalized = coordinator_address.strip().lower() if isinstance(coordinator_address, str) else ""
        try:
            valid_coordinator = (
                len(normalized) == 42
                and normalized.startswith("0x")
                and normalized != "0x" + "0" * 40
                and len(bytes.fromhex(normalized[2:])) == 20
            )
        except ValueError:
            valid_coordinator = False
        if not valid_coordinator:
            _expected("coordinator address is invalid")
        if outcome not in OUTCOMES:
            _expected("outcome must be succeeded or failed")
        self.coordinator_address = normalized
        self.outcome = outcome
        self.processed_effects = u256(0)

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _emit_ack(self, request_id: str, request: ProcessedRequest) -> None:
        coordinator = gl.get_contract_at(Address(self.coordinator_address))
        coordinator.emit(on="finalized").acknowledge(
            request_id,
            request.sequence,
            request.result_code,
            request.result_hash,
        )

    @gl.public.write
    def handle_request(
        self,
        coordinator_address: str,
        request_id: str,
        sequence: u256,
        payload_hash: str,
        expires_at: u256,
    ) -> None:
        coordinator_address = coordinator_address.strip().lower()
        if _address_text(gl.message.sender_address) != self.coordinator_address:
            _expected("Only the configured coordinator may dispatch requests")
        if coordinator_address != self.coordinator_address:
            _expected("Request coordinator does not match worker configuration")
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in request_id)
        ):
            _expected("request id is invalid")
        if not isinstance(payload_hash, str) or int(sequence) == 0 or len(payload_hash) != 66 or not payload_hash.startswith("0x"):
            _expected("request envelope is invalid")
        try:
            bytes.fromhex(payload_hash[2:])
        except ValueError:
            _expected("request envelope is invalid")
        if self._now() >= int(expires_at):
            _expected("Request has expired before worker execution")

        if request_id in self.requests:
            prior = self.requests[request_id]
            if int(prior.sequence) != int(sequence) or prior.payload_hash != payload_hash:
                _expected("Request id was replayed with conflicting content")
            # A retry reuses the same idempotency key. Do not repeat the effect;
            # emit the previously stored result so the coordinator can recover.
            self._emit_ack(request_id, prior)
            return

        result_hash = "0x" + hashlib.sha256(
            f"MessageRailWorker/v1|{request_id}|{int(sequence)}|{payload_hash}|{self.outcome}".encode("utf-8")
        ).hexdigest()
        result = ProcessedRequest(
            sequence=u256(sequence),
            payload_hash=payload_hash,
            result_code=self.outcome,
            result_hash=result_hash,
        )
        self.requests[request_id] = result
        self.processed_effects = u256(int(self.processed_effects) + 1)
        # TEST ONLY: lose the first ack so a retry exercises the stored-result path.
        return

    @gl.public.view
    def get_result(self, request_id: str) -> dict:
        if request_id not in self.requests:
            _expected("Request was not processed")
        request = self.requests[request_id]
        return {
            "request_id": request_id,
            "sequence": int(request.sequence),
            "payload_hash": request.payload_hash,
            "result_code": request.result_code,
            "result_hash": request.result_hash,
            "processed_effects": int(self.processed_effects),
        }
