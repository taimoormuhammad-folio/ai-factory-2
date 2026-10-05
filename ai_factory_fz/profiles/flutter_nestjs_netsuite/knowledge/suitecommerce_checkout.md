SuiteCommerce checkout on this site (VERIFIED against the SB2 site and its website on 2026-10-05, except where
marked). The standard SuiteCommerce flow (payment method sent on every update, no checkout context) does NOT
work here: NetSuite then returns no shipping methods and silently drops the billing address and payment.

Call sequence, all with the session cookies and X-Requested-With: XMLHttpRequest:
1. Open the checkout context before any checkout call (load, update, submit):
   GET {base}/store/services/CheckoutEnvironment.Service.ss?lang=en_CA&cur=CAD&X-SC-Touchpoint=checkout
   &cart-bootstrap=T
   The body is JavaScript, not JSON: ignore it, but keep every Set-Cookie it returns. Treat a failure here as
   non-fatal (log it and continue).
2. Read the order: GET {base}/store/services/LiveOrder.Service.ss?c={c}&n={n}&cur={currency id}&internalid=cart
   cur is the currency's internal id (1 = CAD on this site), from configuration (SUITECOMMERCE_CURRENCY_ID).
   With the context open, shipmethods lists the real methods for the ship address, e.g.
   {"internalid":"40431","name":"2Ship","rate":0,"rate_formatted":"Free!"}, {"internalid":"3","name":"FedEx",
   "rate":1}, {"internalid":"4","name":"UPS","rate":1}.
3. Update the order: PUT the same LiveOrder URL with the whole order object and:
   shipaddress, billaddress (address internalids), "sameAs": true when billing equals shipping,
   shipmethod (one of shipmethods[].internalid), a TOP-LEVEL "purchasenumber" (PO number, may be ""),
   options {custbody_f3_so_shipdate: "yyyy-mm-dd", custbody_f3_so_notes}, and "paymentmethods": [].
   Do not send the invoice payment method on updates: that is what made NetSuite drop the billing address.
4. Ship date and notes are custom fields read by the website through the CustomFields extension:
   GET {base}/store/extensions/SuiteCommerce/CustomFields/1.1.4/services/Checkout.Service.ss?c={c}&n={n}
   &fields=custbody_f3_so_shipdate,custbody_f3_so_notes
   (NOT VERIFIED: how the website writes them; keep sending them in options until confirmed.)
5. Submit (NOT VERIFIED yet): POST the LiveOrder URL with the updated order plus
   paymentmethods [{"type":"invoice","primary":true,"terms":{"internalid": <customer terms id>},
   "purchasenumber": <PO>}]. Expect confirmation {internalid, tranid, confirmationnumber}.
   Never retry a submit; a timeout or unclear result means the order status is unknown (check NetSuite before
   resubmitting). Keep it behind CHECKOUT_SUBMIT_ENABLED.

Defaults, so checkout never stalls on a field NetSuite leaves empty:
- billing address = the shipping address (send sameAs true);
- shipping address = the address marked defaultshipping "T", else the first one;
- shipping method = the first one offered; if none is offered, show one option
  "Standard shipping (arranged by our team)" with the cost "to be confirmed".
Checkout is blocked only when the customer has no address, no payment terms, or an item without a price.

Speed: every checkout call to the real site takes 10 to 15 seconds. Use app timeouts of at least 30 seconds
for checkout updates and 60 seconds for submit, and show progress while saving.

The SuiteCommerce mock (infra/suitecommerce-mock) must follow this flow too: serve CheckoutEnvironment.Service.ss
(a script body that sets a cookie), accept sameAs and a top-level purchasenumber, and return shipping methods
only once the checkout context is open, so tests catch a missing step.
