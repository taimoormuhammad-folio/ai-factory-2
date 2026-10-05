B2B commerce rules for this project (the store is a NetSuite SuiteCommerce B2B site):
- Users are contacts of business customers. They sign in with the email and password they use on the
  website; there is no sign-up in the app (accounts are created and approved by the company).
- The logged-in customer's price comes from NetSuite's customer pricing, not the public list price. If the
  customer price is unknown, show "Price unavailable" and block checkout; never show the public price instead.
- Items have a minimum order quantity and may have a quantity step (e.g. multiples of 2). The quantity
  picker starts at the minimum and moves by the step. NetSuite has the final say: show its message if it
  rejects a quantity.
- The cart is the customer's NetSuite cart, shared with the website: changes on either side appear on both.
  Always reload it from the API instead of keeping a local copy as the truth.
- Stock is shown per item (in stock / out of stock, total available); out-of-stock items may still be
  orderable when NetSuite allows it.
- Checkout: choose a shipping address and billing address from the customer's address book, a shipping
  method, an optional requested ship date, an optional purchase order number and order notes; payment is
  on the customer's account terms. Customers without payment terms cannot check out (show why).
- Show the order total from NetSuite (subtotal, shipping, tax, total). After submission show the order
  confirmation number.
- Money is integer minor units with an ISO 4217 currency code (CAD); never floats in the app.
