# Payments -- integration contract

How a product connects to this service on one VPS. Written so that a **second**
product could do it without asking us anything.

The service is internal: no host port, no nginx route, no public DNS. The only
way in is the shared docker network.

---

## 1. What the seam is made of

Two directions, and they are not symmetric.

**Payments -> product.** Three variables, minted by `payments-deploy.sh install`
and written into the product's `.env`:

| Variable | Value | What it is |
|---|---|---|
| `PAYMENTS_API_URL` | `http://payments-app:8000` | Where the product calls us, on the shared network |
| `PAYMENTS_SERVICE_TOKEN` | 64 hex characters | Presented as `Authorization: Bearer <token>` on every inbound call |
| `PAYMENTS_WEBHOOK_SECRET` | 64 hex characters | Arrives in the `X-Payments-Secret` header of every event we send |

`PAYMENTS_API_URL` and `PAYMENTS_SERVICE_TOKEN` travel **together or not at
all**. A product that receives one without the other is not a product with a
half-configured seam -- it is a product that does not start, because its own
configuration refuses that combination on purpose.

**Product -> payments.** Two values, written into `/opt/payments/.env`:

| Variable | Value | Why the product owns it |
|---|---|---|
| `PRODUCT_ENV_PATH` | absolute path of the product's `.env` | Only the product knows its own layout |
| `PRODUCT_WEBHOOK_URL` | absolute URL of the product's receiver | The path belongs to the product's API, and nothing in this repository names it |

---

## 2. Why installation runs twice

`PRODUCT_ENV_PATH` cannot be passed as a process variable: the CLI *sources*
its env file, so the value has to be **in** that file.

`PRODUCT_WEBHOOK_URL` adds a second reason that is ours alone. It is mandatory
and has no default -- the service validates its configuration at startup and
**refuses to boot** without it, which is deliberate: a payment service that
starts unable to tell anyone about a confirmed payment is worse than one that
does not start. So on the first pass there is nothing to bring up.

```
pass 1  payments install        network, mint secrets, ask for wallet
                                addresses and explorer keys, write /opt/payments/.env,
                                hand the three variables over (or print them),
                                STOP -- PRODUCT_WEBHOOK_URL is empty
        product installer       writes PRODUCT_ENV_PATH and PRODUCT_WEBHOOK_URL
                                into /opt/payments/.env
pass 2  payments install        same steps -- all of them no-ops except the
                                last: builds, starts, waits for health
```

Every step is idempotent, so the second pass repeats the first without undoing
it. **Secrets are minted once**: an existing `/opt/payments/.env` is never
regenerated, and the questions of pass 1 are never asked again. Re-minting the
database password next to a surviving data volume would lock the stack out of
its own database.

If `PRODUCT_ENV_PATH` is empty or points at a file that does not exist, the
hand-over prints the three variables as a block to paste by hand and exits
successfully. That fallback is the whole reason a second product needs nothing
from us.

---

## 3. What the product must do

**Verify after the second pass.** Read your own `.env` and check that all three
`PAYMENTS_*` variables are present and non-empty. A product that starts
unlinked while the installer reports success is the failure this check exists
to prevent -- and with the paired configuration above, "unlinked" is usually
"not started at all".

**Receive events.** `POST` with a JSON body and the `X-Payments-Secret` header,
at the URL you delivered as `PRODUCT_WEBHOOK_URL`:

```json
{
  "invoice_id": "…",
  "product_ref": "…",
  "status": "confirmed",
  "credited_amount_cents": 10000,
  "underpaid": false,
  "occurred_at": "2026-08-28T12:00:00+00:00"
}
```

Four statuses arrive: `confirmed`, `expired`, `attempts_exhausted`, `stalled`.

- `credited_amount_cents` and `underpaid` are **absent**, not `null`, when the
  transition did not set them. Test for the key. `underpaid` is a boolean whose
  false value means something, so falsiness cannot distinguish the three cases.
- `occurred_at` is the moment of the **transition**, never of the delivery.
  Events can arrive minutes late, and for `attempts_exhausted` the transition
  itself can lag the moment the budget ran out.
- Delivery is **at-least-once**. Deduplicate on `(invoice_id, status)`; a
  duplicate costs you nothing and a loss costs you a payment.
- Answer **any 2xx** to accept. Anything else is retried with a doubling delay
  from five seconds to an hour, twelve attempts, about two and a half hours in
  total, after which the event is marked `failed` and **not retried again**.
  There is no automatic replay after that.
- Authenticate by comparing `X-Payments-Secret` against your
  `PAYMENTS_WEBHOOK_SECRET` in constant time, and **reject a blank one before
  comparing**: comparing two empty strings succeeds.
- The receiver must sit **outside your user authentication**. The caller is a
  service holding a shared secret, not a logged-in user with a bearer token.

**Order of installation.** Bring payments up first. The product then starts with
`PAYMENTS_*` already in its environment, with no second restart. The reverse
order costs a restart and, in between, a product that believes it has no
payments backend.

If payments is up before the product's receiver exists, nothing is lost:
delivery is refused, retried, and the event stays pending. But see the retry
window above -- an outage longer than it ends in `failed`.

---

## 4. The network

`aivis-shared`, external, created idempotently by whichever installer runs
first. Both stacks attach to it; the payments containers publish no host ports.

Container names are part of the seam because `PAYMENTS_API_URL` resolves
through docker DNS: `payments-app`, `payments-worker`, `payments-postgres`.

---

## 5. Lifecycle

```
payments-deploy.sh install     mint, ask, hand over, bring up (see §2)
payments-deploy.sh update      git pull --ff-only, rebuild, migrate, restart
payments-deploy.sh restart     bounce app + worker only, wait for health
payments-deploy.sh start       whole stack up, wait for health
payments-deploy.sh stop        whole stack down, volumes kept
payments-deploy.sh logs [svc]  follow
payments-deploy.sh db          dump | restore <file> | migrate
payments-deploy.sh status      compose ps
```

Migrations run inside `payments-app`'s own command before uvicorn, so a healthy
API implies a migrated schema, and the worker waits for that health before its
first tick.

`/opt/payments/.env` lives outside the checkout and `update` never touches it.

## Compose project name

This stack declares its compose project name in `deploy/docker-compose.yml`:
`name: payments`, first line. Its volume names are pinned there too.

Both matter to whoever runs it next to another stack. Compose infers the project
from the directory holding the file when it is not declared, so two services
whose files both live in `deploy/` become **one project**: `docker compose ps`
from either directory lists the other's containers, `up -d` reports the other's
as orphans, and `down --remove-orphans` from either destroys the other. The
inferred name is also the prefix of every volume, so it decides where the data
lands.

A stack added beside this one is expected to do the same.
