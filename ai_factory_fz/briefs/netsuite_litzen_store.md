---
title: Litzen store
risk_tier: M
---
# Brief: Mobile store on our NetSuite site (Litzen)

We sell through our NetSuite SuiteCommerce website, https://litzen.folio3.site/scs/. We want a
mobile app for Android and iOS so our customers' buyers can order on the go. The app uses the same
accounts, prices, cart and orders as the website; NetSuite stays the system of record.

Buyers should be able to:
- see a branded splash screen while the app starts
- sign in with the email and password they already use on our website (no sign-up in the app)
- browse and search our items, with picture, name, SKU, stock status and their own customer price
- open an item to see its photos, full description, availability, minimum order quantity and price
- add items to the cart and change quantities, respecting minimum quantities and quantity steps
- check out: pick shipping and billing addresses from their account, choose a shipping method, add an
  optional purchase order number, requested ship date and notes, review the totals (subtotal, shipping,
  tax, total) and place the order on their account terms, then see the order confirmation number
- sign out

The cart is the same one they see on the website. Customers without payment terms cannot check out and
should be told why. The app must look professional and clean, be fast, and handle bad connections and
expired sessions gracefully. Prices are in CAD.

Out of scope for the first version: card payments, quotes, order history, saved lists, returns, the agent
"select a customer" view, push notifications.

## Site settings (checked read-only against the live site; use exactly these, never invent others)

- Site origin (SUITECOMMERCE_BASE_URL): `https://litzen.folio3.site`
- Application path (SUITECOMMERCE_APP_PATH): `scs`. The site's services live under `/scs/`, for example
  `/scs/services/Profile.Service.ss`, `/scs/services/LiveOrder.Service.ss`, `/scs/logOut.ssp`, `/scs/shopping.ssp`,
  `/scs/checkout.ssp`. The earlier site used `store`; this one does not serve `/store/...` (404).
- Company id (SUITECOMMERCE_COMPANY_ID): `7227962_SB2`; site id (SUITECOMMERCE_SITE_ID): `2`; locale CA, CAD, English.
- No catalog filter is given (SUITECOMMERCE_CATALOG_FILTER stays empty). The public items API
  `GET https://litzen.folio3.site/api/items?c=7227962_SB2&n=2` works and returns about 2,950 items.
- NOT verified yet on this site (a person must check them against the real site, the mock cannot): sign-in, the
  cart and checkout services, customer-specific pricing (the earlier site's Folio3 pricing extension is NOT present
  at `/scs_ss2/...`; use the customer price the items API returns for a signed-in session and verify it), and order
  submission. Order submission stays OFF (CHECKOUT_SUBMIT_ENABLED=false) until a person has verified it.
