API error contract (backend and QA):
- Every error body is {statusCode, error, message}. error is the stable code from the contract; message is shown
  to the buyer as is.
- The API's own coded errors keep their message even for 5xx: CHECKOUT_SUBMIT_DISABLED (503, "Order submission
  is switched off on this server."), ORDER_STATUS_UNKNOWN (502), UPSTREAM_UNAVAILABLE (502). Only unexpected
  errors (crashes, unknown exceptions) get the generic "Something went wrong. Please try again later." with the
  details in the server log only. An exception filter that replaces every 5xx message hides these from the app.
- NetSuite business errors (e.g. "The minimum quantity for this item is 3.") come back as 400 NETSUITE_REJECTED
  with NetSuite's message; never show raw NetSuite payloads.
- The app maps the error code first, then the status, and shows the API's message when there is one.
- QA checks, for every error code in the contract, that the app shows its message (not a generic one).
