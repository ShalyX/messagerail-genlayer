# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


ERROR_EXPECTED = "[EXPECTED]"
ZERO_ADDRESS = "0x" + "0" * 40
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
RETRY_DELAY_SECONDS = 30
MAX_DELIVERIES = 3
MAX_REQUEST_ID_LENGTH = 64
OUTCOMES = ("succeeded", "failed")
TERMINAL_STATES = ("succeeded", "failed", "expired")


@allow_storage
@dataclass
class Request:
    request_id: str
    sequence: u256
    owner: Address
    recipient_address: str
    payload_hash: str
    created_at: u256
    expires_at: u256
    last_attempt_at: u256
    attempts: u256
    state: str
    result_code: str
    result_hash: str
    acknowledged_at: u256


def _expected(message: str) -> None:
    raise gl.vm.UserError(f"{ERROR_EXPECTED} {message}")


def _address_text(value) -> str:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    try:
        return value.as_hex.lower()
    except AttributeError:
        return str(value).strip().lower()


def _valid_hex(value: str, byte_length: int) -> bool:
    if not isinstance(value, str) or len(value) != 2 + byte_length * 2 or not value.startswith("0x"):
        return False
    try:
        bytes.fromhex(value[2:])
        return True
    except ValueError:
        return False


def _require_request_id(value: str) -> str:
    if not isinstance(value, str):
        _expected("request id is required")
    value = value.strip()
    if not value or len(value) > MAX_REQUEST_ID_LENGTH:
        _expected("request id must contain 1 to 64 characters")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value):
        _expected("request id contains unsupported characters")
    return value


def _require_address(value: str, field_name: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _valid_hex(normalized, 20) or normalized == ZERO_ADDRESS:
        _expected(f"{field_name} address is invalid")
    return normalized


def _require_hash(value: str, field_name: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not _valid_hex(normalized, 32):
        _expected(f"{field_name} must be 32-byte hex")
    return normalized


class MessageRail(gl.Contract):
    """Tracks asynchronous IC-to-IC requests through a recipient acknowledgement."""

    requests: TreeMap[str, Request]
    next_sequence: u256

    def __init__(self):
        self.next_sequence = u256(1)

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _get_request(self, request_id: str) -> Request:
        request_id = _require_request_id(request_id)
        if request_id not in self.requests:
            _expected("Request does not exist")
        return self.requests[request_id]

    def _emit_request(self, request: Request) -> None:
        recipient = gl.get_contract_at(Address(request.recipient_address))
        coordinator_address = _address_text(gl.message.contract_address)
        recipient.emit(on="finalized").handle_request(
            coordinator_address,
            request.request_id,
            request.sequence,
            request.payload_hash,
            request.expires_at,
        )

    @gl.public.write
    def create_request(
        self,
        request_id: str,
        recipient_address: str,
        payload_hash: str,
        timeout_seconds: u256,
    ) -> None:
        request_id = _require_request_id(request_id)
        recipient_address = _require_address(recipient_address, "recipient")
        payload_hash = _require_hash(payload_hash, "payload hash")
        timeout = int(timeout_seconds)
        if timeout < MIN_TIMEOUT_SECONDS or timeout > MAX_TIMEOUT_SECONDS:
            _expected("timeout must be between 60 seconds and 7 days")
        if request_id in self.requests:
            _expected("Request id has already been used")

        now = self._now()
        sequence = self.next_sequence
        self.next_sequence = u256(int(sequence) + 1)
        request = Request(
            request_id=request_id,
            sequence=sequence,
            owner=gl.message.sender_address,
            recipient_address=recipient_address,
            payload_hash=payload_hash,
            created_at=u256(now),
            expires_at=u256(now + timeout),
            last_attempt_at=u256(now),
            attempts=u256(1),
            state="awaiting_ack",
            result_code="",
            result_hash="",
            acknowledged_at=u256(0),
        )
        self.requests[request_id] = request
        self._emit_request(request)

    @gl.public.write
    def retry_request(self, request_id: str) -> None:
        request = self._get_request(request_id)
        if gl.message.sender_address != request.owner:
            _expected("Only the request owner may retry delivery")
        if request.state != "awaiting_ack":
            _expected("Request is not awaiting acknowledgement")
        now = self._now()
        if now >= int(request.expires_at):
            _expected("Request deadline has passed")
        if int(request.attempts) >= MAX_DELIVERIES:
            _expected("Maximum delivery attempts reached")
        if now < int(request.last_attempt_at) + RETRY_DELAY_SECONDS:
            _expected("Retry delay has not elapsed")

        request.attempts = u256(int(request.attempts) + 1)
        request.last_attempt_at = u256(now)
        # Retries keep the original sequence and payload commitment. The receiver
        # must treat that pair as an idempotency key and replay its saved receipt.
        self._emit_request(request)

    @gl.public.write
    def acknowledge(
        self,
        request_id: str,
        sequence: u256,
        result_code: str,
        result_hash: str,
    ) -> bool:
        request = self._get_request(request_id)
        if _address_text(gl.message.sender_address) != request.recipient_address:
            _expected("Only the registered recipient may acknowledge")
        if int(sequence) != int(request.sequence):
            _expected("Acknowledgement sequence does not match")
        if result_code not in OUTCOMES:
            _expected("Acknowledgement outcome is invalid")
        result_hash = _require_hash(result_hash, "result hash")

        if request.state in TERMINAL_STATES:
            if request.state == result_code and request.result_hash == result_hash:
                return True
            if request.state in OUTCOMES:
                _expected("Conflicting acknowledgement for a terminal request")
            _expected("Request is not awaiting acknowledgement")
        if request.state != "awaiting_ack":
            _expected("Request is not awaiting acknowledgement")
        now = self._now()
        if now >= int(request.expires_at):
            _expected("Request deadline has passed")

        request.state = result_code
        request.result_code = result_code
        request.result_hash = result_hash
        request.acknowledged_at = u256(now)
        return True

    @gl.public.write
    def expire_request(self, request_id: str) -> None:
        request = self._get_request(request_id)
        if request.state != "awaiting_ack":
            _expected("Request is not awaiting acknowledgement")
        if self._now() < int(request.expires_at):
            _expected("Request deadline has not passed")
        request.state = "expired"

    @gl.public.view
    def get_request(self, request_id: str) -> dict:
        request = self._get_request(request_id)
        return {
            "request_id": request.request_id,
            "sequence": int(request.sequence),
            "owner": _address_text(request.owner),
            "recipient_address": request.recipient_address,
            "payload_hash": request.payload_hash,
            "created_at": int(request.created_at),
            "expires_at": int(request.expires_at),
            "attempts": int(request.attempts),
            "state": request.state,
            "result_code": request.result_code,
            "result_hash": request.result_hash,
            "acknowledged_at": int(request.acknowledged_at),
        }
