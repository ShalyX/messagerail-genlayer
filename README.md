# MessageRail

MessageRail is a reusable GenLayer contract for coordinating asynchronous work between Intelligent Contracts.

It solves a small but important integration problem: emitting an IC-to-IC message does not mean the child operation has completed. MessageRail records a request, emits it to one configured recipient on parent finalization, and records a separate acknowledgement from that exact recipient. Retries reuse the same sequence and payload commitment, so an idempotent receiver can return its saved receipt without performing the operation twice.

## A real use case

A commerce coordinator sends an order instruction to a fulfillment Intelligent Contract. The fulfillment contract records the instruction and returns a receipt in a later finalized message. The coordinator can distinguish `succeeded`, `failed`, and `expired` instead of treating “message emitted” as “order fulfilled.” If the receipt is lost, the coordinator can retry the same request key.

The included `IdempotentWorker` is a small reference receiver. It stores one result per request and re-emits the stored result for an exact duplicate.

## Lifecycle

```text
create_request
      │ parent reaches finality; child request is emitted
      ▼
awaiting_ack ─── recipient acknowledgement ───► succeeded | failed
      │
      ├── retry after 30 seconds, same sequence + payload hash (up to 3 sends)
      └── expire after deadline ────────────────► expired
```

`expired` means no acceptable acknowledgement arrived before the deadline. It does not prove that the child did not execute. A receiver must make effects idempotent, or otherwise safe to repeat, before its coordinator uses retries.

## Contract surface

`MessageRail`:

- `create_request(request_id, recipient_address, payload_hash, timeout_seconds)` stores a one-use request and emits `handle_request(...)` to the recipient on finality.
- `retry_request(request_id)` is owner-only, rate-limited, bounded to three deliveries, and reuses the original request sequence and payload hash.
- `acknowledge(request_id, sequence, result_code, result_hash)` accepts `succeeded` or `failed` only from the registered recipient. Exact duplicate acknowledgements are idempotent; conflicting ones fail.
- `expire_request(request_id)` closes a still-waiting request after its deadline.
- `get_request(request_id)` returns the public lifecycle record.

`IdempotentWorker` is an example implementation of the receiver-side contract. Production receivers should adapt its sender check, request-key storage, and acknowledgement emission to their own effects.

## Trust boundary

MessageRail proves that the registered recipient contract sent a receipt for the registered request sequence and payload commitment. It does not prove that the recipient performed its claimed work correctly, inspect external side effects, or roll back an already-finalized child transaction. `payload_hash` is a caller-supplied commitment; the coordinator does not fetch the underlying payload. These guarantees must stay explicit in any consuming application.

## Test

```powershell
python -m pip install -r requirements.txt
python -m pytest -p no:cacheprovider tests/direct tests/integration -q
python -m genvm_linter.cli check --json contracts/message_rail.py
python -m genvm_linter.cli check --json examples/idempotent_worker.py
python -m genvm_linter.cli check --json examples/adversarial_ack_worker.py
python -m genvm_linter.cli check --json examples/delivery_cap_test_worker.py
```

The direct tests cover malformed requests, unique IDs, caller and sequence binding, duplicate/conflicting receipts, retry limits, cooldowns, and expiry. Integration tests execute the two-contract message path in the GenLayer simulator, including lost acknowledgements, idempotent retries, wrong-sequence callbacks, conflicting terminal receipts, and late acknowledgements after expiry.

## Deployment status

A test-only `IdempotentWorker` variant ([`examples/lost_ack_test_worker.py`](examples/lost_ack_test_worker.py)) was deployed to StudioNet on Oct 20, 2026 at [`0x03f1788CEC48f4E2e5b405950449c323137C9d6a`](https://explorer-studio.genlayer.com/address/0x03f1788CEC48f4E2e5b405950449c323137C9d6a). It stores a request result and intentionally omits the first acknowledgement; an exact retry returns the saved receipt.

The live request `lostack-live-20261020-1358` exercised that path through coordinator [`0xE5F38690EC9DaA32Ab12c1880aFF8577FF163F6b`](https://explorer-studio.genlayer.com/address/0xE5F38690EC9DaA32Ab12c1880aFF8577FF163F6b), deployed in [this StudioNet transaction](https://explorer-studio.genlayer.com/tx/0xe981c75784616ea2055317375de3b5d3d187c38b36b06125c3e067ef1f786d26): the first delivery finalized without an acknowledgement, the retry delivered the same request ID, sequence, and payload hash, and the worker acknowledgement finalized. Final contract readback showed coordinator state `succeeded` with 2 attempts and worker `processed_effects: 1`, confirming that this worker did not repeat its stored state change on retry.

Transaction evidence: [worker deployment](https://explorer-studio.genlayer.com/tx/0x87f9dc01a034a8341b2e45e311df0c6e46ac82d90c142f792d7b0cc7554c831f), [request creation](https://explorer-studio.genlayer.com/tx/0xbdc4998a1890957a44128fe0d120dfb6f1250f1e86c11de7bc1faeed125c1812), [first delivery](https://explorer-studio.genlayer.com/tx/0x9c320761f52db35b26f7b32c7da00de317209078f1a3813073e98ef32348bbc4), [retry](https://explorer-studio.genlayer.com/tx/0xb856a4e803b2b73821b0b89ef7c5e68f48444e901e999e707088600fb1cee7ec), [duplicate delivery](https://explorer-studio.genlayer.com/tx/0x88980b97664e8b3b968b16a65bce8bfa339d857e3eefa6dd5e6387a234ee054a), [acknowledgement](https://explorer-studio.genlayer.com/tx/0x87bc2e6941d355fa05bc6f5fe9f6f346664b894c69a6749b729bfacf12256f89). This validates the reference test worker's persisted state, not arbitrary external side effects.

The first failed-outcome attempt, `failure-live-20261020-01`, remains an expired-request test: its 300-second deadline passed before retry, and the retry rolled back with `[EXPECTED] Request deadline has passed`. It does not count as a failed-result replay.

The corrected request `failure-live-20261020-02` used the same failed-outcome worker with a 3,600-second deadline. The [request creation](https://explorer-studio.genlayer.com/tx/0x04e0937bc78f326354771b9f784fc7d267b9de98f1a1a54323cc2a315c23a0ec), [first delivery](https://explorer-studio.genlayer.com/tx/0xedea551bb9560baef7d181018f6e8234e736d51cae0dd0c3028b470af779a0c3), [retry](https://explorer-studio.genlayer.com/tx/0xf215f4d23b3076827614063631d33254b8acd65f6f918a5b75f5cd623233fc9b), [duplicate delivery](https://explorer-studio.genlayer.com/tx/0xbbfb81e475b1017298d1f76b68c70d962df06d2267badc66b74b260e142cbd00), and [acknowledgement](https://explorer-studio.genlayer.com/tx/0x8220903f9a296e115decbb5b4e15243b7e32457366a07e05932deb4e925ebac8) all finalized. The coordinator readback reached `state: failed`, `result_code: failed`, and `attempts: 2`; its result hash was `0xd5e2d5535ae4c0f199554f88d4913c946e278323a3df8c782e831e9b9844b75b`. The worker returned the same stored result and its global `processed_effects` remained `2` across retry, confirming the duplicate delivery did not repeat its effect. This is verified behavior for the test receiver, not a guarantee about arbitrary external side effects.

The test-only [`DeliveryCapTestWorker`](examples/delivery_cap_test_worker.py) is deployed at [`0xb3ecC3702c57a42827dc12Be0aDECf042C458675`](https://explorer-studio.genlayer.com/address/0xb3ecC3702c57a42827dc12Be0aDECf042C458675) in [this StudioNet deployment transaction](https://explorer-studio.genlayer.com/tx/0xdeec2dfe03d227d4171978f6d618b26de325490bf06d9cd0e1b9375f6ab37294). In live request `delivery-cap-live-20261020-02` (sequence 13), the first three deliveries finalized to this real contract receiver. Receiver readback stayed at `processed_effects: 2` with the same saved receipt after the two retries. The [fourth `retry_request`](https://explorer-studio.genlayer.com/tx/0x621fcda94262dde9ab413442bcd7b3de08beb897bdf934c51eb9debd6af691ad) finalized as a rollback with `[EXPECTED] Maximum delivery attempts reached`; all five validators agreed. The cap rejection occurs before another child delivery, and the receiver effect count remained unchanged. This is a StudioNet test of the configured retry bound and this test receiver's idempotent persistence, not a guarantee about arbitrary external side effects.

The standard [`IdempotentWorker`](examples/idempotent_worker.py) is deployed at [`0xffE2536E5E5E69f1f187b72E1770db04B9D7034D`](https://explorer-studio.genlayer.com/address/0xffE2536E5E5E69f1f187b72E1770db04B9D7034D), in [deployment transaction `0xc116c185…18e46736`](https://explorer-studio.genlayer.com/tx/0xc116c185dc9548d27523ea259658912bd7518f8993f33e82b9e0e29a18e46736). Live request `standard-live-20261020-01` (sequence 14) finalized its [request transaction](https://explorer-studio.genlayer.com/tx/0xd6e391781be6d4cb164070bdbdb43dece1d28ca481db3e481fdf0a9200593824), [worker dispatch](https://explorer-studio.genlayer.com/tx/0xfa66a9838a6262795d0291419cdec5d458826201617c468fbd76f00005b8a709), and [worker acknowledgement](https://explorer-studio.genlayer.com/tx/0xd09d9ecb5086bd1309189b739f7d3dc36cc69f0dd5a4e9fddb8def56986f96a3). Coordinator and worker readbacks agreed on `result_code: failed`, payload hash `0x1111…1111`, result hash `0x5551bcb116080645fe129c1d7ec9db5174c603fc151b996d0ea29a6defc04393`, and sequence 14; the worker's synthetic `processed_effects` counter was 1. This validates the configured failure receipt path, not a real external business effect.

The test-only [`AdversarialAckWorker`](examples/adversarial_ack_worker.py) is deployed at [`0x16F2A917dEbB5FA36d5bd9Bb92992Fb20c534025`](https://explorer-studio.genlayer.com/address/0x16F2A917dEbB5FA36d5bd9Bb92992Fb20c534025), in [deployment transaction `0xc7a902d2…07b5d858`](https://explorer-studio.genlayer.com/tx/0xc7a902d27af44c7c059bf47410a95fe6ab4ae607f753884834b0b9dd07b5d858). Live StudioNet checks finalized as expected rollbacks: [wrong sequence](https://explorer-studio.genlayer.com/tx/0x5003acaf75e8b1996676dd341b01690d03d3dd09d1879b9568fe9cf0e9398551), [conflicting terminal receipt](https://explorer-studio.genlayer.com/tx/0x7219fa2b68fed241b1662c54856f4d795798afd84fb6ba23c738bda0f0c5566d), and [late acknowledgement after expiry](https://explorer-studio.genlayer.com/tx/0x9356d2ed3d4353ab63cf2b53ce80652947a832306cf63775500f29d33ff8a7a0). A separate [wrong-caller acknowledgement](https://explorer-studio.genlayer.com/tx/0x19d17c570ab8e96ad7e934b28f0a3dfe57f6301bd9426fd37b0e104dd72f9c75) also rolled back. Coordinator readback confirmed the request used for both caller/sequence attacks remained `awaiting_ack` with one delivery and no result; the conflicting attempt left its original `succeeded` receipt and hash intact; and the expired request stayed `expired` with no result after the late callback. The owner-triggered [`expire_request`](https://explorer-studio.genlayer.com/tx/0x63413d4f6b80b2eb00630f13e99f67bc6ca3a33e08ff2ee386eaa33096b735cf) finalized successfully before the late-callback check.

## Repository scope

This is a contract-focused repository: contracts, an example receiver, tests, public documentation, and contract tooling only. It contains no frontend, wallet material, portal data, or private submission checklist; deployment and test evidence is documented separately.
