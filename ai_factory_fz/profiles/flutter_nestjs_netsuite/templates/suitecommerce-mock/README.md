# SuiteCommerce mock (staging and tests only)

A stand-in for the NetSuite SuiteCommerce site, so staging, smoke tests and device tests run without the
real site or real credentials. Production points `SUITECOMMERCE_BASE_URL` at the real site instead.

Run: `node server.js` (Node 18+, no dependencies) or build the Dockerfile. Listens on `PORT` (default 8080);
`GET /health` returns 200.

Test users (made-up data in `fixtures/customer.json`):

| Email | Password | Notes |
|---|---|---|
| buyer@example.com | demo-password-1 | Has payment terms (Net 30), two addresses |
| noterms@example.com | demo-password-2 | No payment terms: checkout must be refused |

Items (`fixtures/items.json`) are real sandbox catalog items, trimmed. `_mock` holds what the real site
computes elsewhere: the customer's price, whether the item is sold to the customer's subsidiary (149, 18257
and 52961 are not, so adding them fails like on the real site), and the quantity step (item 93: multiples of 2).

What matches the real site (verified): login needs POST and `X-Requested-With: XMLHttpRequest`, and those
two errors come back with HTTP 200 and the error in the body; session cookies; item list/detail shapes
and `sort` restriction; customer pricing (fails without a session); cart read (empty guest cart without a
session), add, update and delete with their minimum-quantity and subsidiary errors.

What is assumed (the real checkout could not be observed): order update with `PUT LiveOrder.Service.ss`,
submit with `POST LiveOrder.Service.ss` returning `confirmation`, shipping methods appearing once a
shipping address is set, payment as `{"type":"invoice","terms":{"internalid":...},"purchasenumber":...}`,
and the HTTP status of business errors. Changing a cart without a session returns 401 here (not mocked).

State is in memory: restarting the mock empties carts and sessions. `MOCK_SESSION_TTL_S` (default 1200)
sets the session timeout.
