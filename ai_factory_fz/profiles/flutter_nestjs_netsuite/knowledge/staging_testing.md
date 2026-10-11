Staging and test environment (SuiteCommerce mock):
- Staging never calls the real NetSuite site. It runs infra/suitecommerce-mock (see its README.md), and the
  API reaches it through SUITECOMMERCE_BASE_URL. The mock follows the real response shapes.
- Test users on the mock (made-up data, safe to use in tests and smoke suites):
  buyer@example.com / demo-password-1 (has Net 30 payment terms and two addresses: 501 shipping, 502 billing)
  noterms@example.com / demo-password-2 (no payment terms: checkout must be refused with a clear message).
  Read them from environment variables in tests where possible (e.g. SMOKE_USER_EMAIL, SMOKE_USER_PASSWORD)
  with these values as the defaults.
- Useful mock items: 197 (min quantity 5, customer price 500.00), 93 (min 10, multiples of 2, price 13.00),
  196 (price 139.50); 149, 18257 and 52961 are listed but cannot be added ("Invalid item reference key").
- The mock keeps carts in memory per customer and empties a cart after an order is submitted. Smoke tests
  must start by emptying the cart through the API so earlier runs do not affect them.
- Journeys worth covering end to end: health; sign in (and wrong password); list and search items; item
  detail with the customer price; add to cart, change quantity (minimum and step errors), remove; checkout
  with addresses, shipping method, PO number and terms, then the confirmation number; sign out.
