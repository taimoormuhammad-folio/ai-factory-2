# Brief: B2B mobile store on our NetSuite site

We sell lighting products to business customers through our NetSuite SuiteCommerce website,
https://stanpro2.folio3.site. We want a
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
