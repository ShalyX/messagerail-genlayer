# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


ERROR_EXPECTED = "[EXPECTED]"
MODES = ("hold", "wrong_sequence", "succeed")


@allow_storage
@dataclass
class StoredReceipt:
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


class AdversarialAckWorker(gl.Contract):
    """TEST ONLY receiver for coordinator callback authorization and lifecycle cases."""

    coordinator_address: str
    owner_address: str
    mode: str
    processed_effects: u256
    requests: TreeMap[str, StoredReceipt]

    def __init__(self, coordinator_address: str, mode: str = "hold"):
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
        if mode not in MODES:
            _expected("mode is invalid")

        self.coordinator_address = normalized
        self.owner_address = _address_text(gl.message.sender_address)
        self.mode = mode
        self.processed_effects = u256(0)

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _only_owner(self) -> None:
        if _address_text(gl.message.sender_address) != self.owner_address:
            _expected("Only the test worker owner may invoke this method")

    def _get_receipt(self, request_id: str) -> StoredReceipt:
        if request_id not in self.requests:
            _expected("Request was not received")
        return self.requests[request_id]

    def _emit_ack(
        self,
        request_id: str,
        receipt: StoredReceipt,
        sequence: u256,
        result_code: str,
        result_hash: str,
    ) -> None:
        coordinator = gl.get_contract_at(Address(self.coordinator_address))
        coordinator.emit(on="finalized").acknowledge(
            request_id,
            sequence,
            result_code,
            result_hash,
        )

    def _emit_saved_ack(self, request_id: str, receipt: StoredReceipt) -> None:
        self._emit_ack(
            request_id,
            receipt,
            receipt.sequence,
            receipt.result_code,
            receipt.result_hash,
        )

    @gl.public.write
    def set_mode(self, mode: str) -> None:
        self._only_owner()
        if mode not in MODES:
            _expected("mode is invalid")
        self.mode = mode

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
        if not isinstance(payload_hash, str) or len(payload_hash) != 66 or not payload_hash.startswith("0x"):
            _expected("Request envelope is invalid")
        try:
            bytes.fromhex(payload_hash[2:])
        except ValueError:
            _expected("Request envelope is invalid")
        if int(sequence) == 0:
            _expected("Request envelope is invalid")
        if self._now() >= int(expires_at):
            _expected("Request has expired before worker execution")

        if request_id in self.requests:
            receipt = self.requests[request_id]
            if int(receipt.sequence) != int(sequence) or receipt.payload_hash != payload_hash:
                _expected("Request id was replayed with conflicting content")
            self._emit_saved_ack(request_id, receipt)
            return

        result_hash = "0x" + hashlib.sha256(
            f"MessageRailAdversarialWorker/v1|{request_id}|{int(sequence)}|{payload_hash}".encode("utf-8")
        ).hexdigest()
        receipt = StoredReceipt(
            sequence=u256(sequence),
            payload_hash=payload_hash,
            result_code="succeeded",
            result_hash=result_hash,
        )
        self.requests[request_id] = receipt
        self.processed_effects = u256(int(self.processed_effects) + 1)

        if self.mode == "hold":
            return
        if self.mode == "wrong_sequence":
            self._emit_ack(
                request_id,
                receipt,
                u256(int(receipt.sequence) + 1),
                receipt.result_code,
                receipt.result_hash,
            )
            return
        self._emit_saved_ack(request_id, receipt)

    @gl.public.write
    def send_ack(self, request_id: str) -> None:
        self._only_owner()
        self._emit_saved_ack(request_id, self._get_receipt(request_id))

    @gl.public.write
    def send_conflicting_ack(self, request_id: str) -> None:
        self._only_owner()
        receipt = self._get_receipt(request_id)
        conflicting_code = "failed" if receipt.result_code == "succeeded" else "succeeded"
        first_nibble = "0" if receipt.result_hash[2] != "0" else "1"
        conflicting_hash = "0x" + first_nibble + receipt.result_hash[3:]
        self._emit_ack(
            request_id,
            receipt,
            receipt.sequence,
            conflicting_code,
            conflicting_hash,
        )

    @gl.public.view
    def get_result(self, request_id: str) -> dict:
        receipt = self._get_receipt(request_id)
        return {
            "request_id": request_id,
            "sequence": int(receipt.sequence),
            "payload_hash": receipt.payload_hash,
            "result_code": receipt.result_code,
            "result_hash": receipt.result_hash,
            "processed_effects": int(self.processed_effects),
        }
