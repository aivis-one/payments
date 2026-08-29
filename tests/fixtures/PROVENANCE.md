# Where these responses came from

**Base envelopes captured from the live APIs on 2026-08-28; everything built on
top of them is still reconstruction.** Read that sentence as two claims,
because they are not the same strength.

The owner ran the calls against Etherscan V2 and TronScan and handed the
answers over. Both adapters were then run against the un-anonymised captures:
`matched`, `wrong_address`, `not_found` on a foreign issuing contract,
confirmation depth and amount conversion all came out as this code already
produced them. **The captures confirmed the parsing; they did not change it.**

What was captured is the SHAPE. Addresses, hashes and amounts in the captured
material were anonymised before it reached this repository, so the values in
these files are the ones the tests were written against and the fields are the
ones the live API sends. Merging shape into existing files rather than replacing
them was deliberate: two sets of invented values are equally invented, and
overwriting would have rewritten the arithmetic of a large part of the suite for
nothing.

| File | Status |
|---|---|
| `tronscan_not_found.json` | **Confirmed by observation.** TronScan really does answer a bare `{}` for a hash it has never seen. The reconstruction was exactly right and the file is unchanged. |
| `tronscan_usdt_single.json` | Live shape merged in: `contract_map`, `contractInfo`, `trigger_info`, `tokenTransferInfo`, `transfersAllList`, `srConfirmList`, `normalAddressInfo`, `contractData`, `toAddress` and a dozen more. |
| `etherscan_erc20_single.json` | Live shape confirmed; `blockTimestamp` added to the log entry, which the reconstruction lacked. |
| `etherscan_block_number.json` | Live shape confirmed. |
| `etherscan_notok_invalid_key.json` | New, and the only NOTOK text ever observed: the capture was taken with an invalid key. |
| `etherscan_notok_rate_limit.json` | **Unchanged, and its text is still reconstruction.** The envelope around it is confirmed; the wording of the rate-limit message was never seen, because nobody provoked a rate limit. |
| Everything else -- `split`, `mixed`, `foreign_only`, `wrong_recipient`, `reverted`, `trx_transfer`, `no_transfer_info`, `approval_only`, `jsonrpc_error`, `result_null`, `no_logs`, `bsc20_single` | **Still reconstruction.** Derived from the confirmed envelopes, but no such response was ever observed. |

That last row is the point of this table. What was confirmed is the base form,
not every case built from it.

## Three things the capture settled

**`toAddress` at the top level is the CONTRACT, not the recipient.** For a TRC20
transfer the transaction is addressed to the token; the recipient lives only
inside `trc20TransferInfo[].to_address`. The field was absent from the
reconstruction entirely, so nothing here was ever wrong -- but an adapter
written against the obvious top-level field would answer `wrong_address` to
every genuine payment. It is now present in the fixture and asserted in
`test_the_top_level_to_address_is_the_contract_and_is_not_read`.

**`trc20TransferInfo` is an array, `amount_str` is a string, `confirmations` is
a top-level integer.** All three as reconstructed.

**The NOTOK envelope is real, and only one of its two texts is.** Etherscan
answers an invalid key with HTTP 200 and its own `status`/`message`/`result`
envelope rather than JSON-RPC, which is what the adapter keys on. The
rate-limit wording sitting in the other file was never observed and stays a
reconstruction; the parametrised test asserts the presence of a top-level
`status` and never the text, so a third wording would need no change here.

**Etherscan emits the issuing contract in lower case** while the config carries
the EIP-55 checksummed form. The case trap is real: a literal comparison would
answer `not_found` to every genuine ERC20 payment.

## Two things it did not settle

**The depth convention (±1 block).** A single sample with a large confirmation
count cannot distinguish an off-by-one, and the owner closed the question as
immaterial -- three seconds against a threshold of twenty. Still unknown, and
recorded as unknown.

**One anonymised address in the handed-over material did not survive
base58check.** The sender in the TronScan capture had been typed by hand during
anonymisation and its checksum did not hold. It was not merged: the tree's own
sender, which is valid, stayed. A fixture that contradicts a validator living
one module away is a trap set for whoever next decides to check `from_address`.

`etherscan_block_number.json` arrived in H4 as documentation and is now
confirmed: the JSON-RPC shape is what the live endpoint sends, and the value in
it stays chosen so that the depth against the receipt fixtures comes out to a
readable number. TronScan's top-level `confirmations`, which the worker reads
straight out of the transaction record, is confirmed by the capture as well.
Three deliveries running were written against documentation here; this one is
the first written against an answer.

Deliberately malformed payloads -- a receipt with no `logs` key, topics of the
wrong length, an unreadable amount -- are **not** stored here. They are built
inline in the test that uses them, next to the sentence explaining what is
broken and why, because no real explorer would ever send them and a file
purporting to be a frozen response would be claiming otherwise.
