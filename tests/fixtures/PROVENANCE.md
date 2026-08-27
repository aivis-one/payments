# Where these responses came from

**Reconstructed from published documentation. Not captured from a live probe.**

Nobody ran these calls against Etherscan or TronScan. The envelopes, field
names and value encodings here were assembled from each explorer's API
documentation and from the response samples published in it. They are faithful
to the documented contract and to nothing else.

That matters for exactly one thing: they cannot vouch for undocumented
behaviour. Two answers in particular are the ones this service most needs to
get right, and both are the ones documentation is worst at describing --
because neither is a success case anybody writes an example for:

- `tronscan_not_found.json` -- what TronScan actually returns for a hash it has
  never seen. Modelled here as `{}`.
- `etherscan_notok_rate_limit.json` -- Etherscan's own error envelope arriving
  with HTTP 200 on a `module=proxy` URL that otherwise speaks JSON-RPC.

If either of those is wrong, the `not_found` / `api_error` boundary moves, and
that boundary decides whether a user's attempt is spent. Real captures of both
have been requested from the owner. When they arrive, replace the two files and
re-run; the tests read the files and assert on verdicts, so a faithful capture
should need no test edits.

Everything else here is combinatorial content built on top of those envelopes
-- one transfer, several transfers, ours mixed with a foreign token, a transfer
to the wrong address. Those shapes are not in question; which contract emitted
a transfer and who received it is plain in both APIs.

H4 added one more envelope, `etherscan_block_number.json`, on the same terms:
the JSON-RPC shape of `eth_blockNumber` is documented, the value in it is
chosen so that the depth against the receipt fixtures comes out to a readable
number. TronScan's `confirmations` field, which the worker reads straight out
of the transaction record, is documented too and likewise unprobed -- it is the
third delivery running that we write against documentation rather than a
capture, and the open item belongs to the owner rather than to any of them.

Deliberately malformed payloads -- a receipt with no `logs` key, topics of the
wrong length, an unreadable amount -- are **not** stored here. They are built
inline in the test that uses them, next to the sentence explaining what is
broken and why, because no real explorer would ever send them and a file
purporting to be a frozen response would be claiming otherwise.
