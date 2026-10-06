# SuiteCommerce sites differ: read this first

- `{base}` is the site origin (SUITECOMMERCE_BASE_URL, e.g. https://litzen.folio3.site). `{app}` is the site's
  application path, a SETTING (SUITECOMMERCE_APP_PATH): `store` on stanpro2.folio3.site, `scs` on litzen.folio3.site.
  Every `{base}/{app}/...` URL below uses it; never hard-code `store`. The public items API `{base}/api/items`
  (and `/api/personalized/items`) is at the origin on both sites.
- The examples below were VERIFIED on stanpro2.folio3.site. On another site, anything under `{base}/{app}/` and every
  extension (such as the Folio3 customer-pricing module) must be checked against that site before it is relied on:
  a 404 means it is not there. Do not copy a path or a field from the examples without proof.
- litzen.folio3.site, checked read-only: account `7227962_SB2`, site `n=2`, locale CA/CAD; `GET /api/items` works
  (2,950 items; fields such as onlinecustomerprice_detail and itemimages_detail); services answer under `/scs/services/`
  (Profile, LiveOrder), `/scs/logOut.ssp`, `/scs/shopping.ssp`, `/scs/checkout.ssp`. The customer-pricing extension is
  NOT at `/scs_ss2/...` (404): there, use the customer price the items API returns for a signed-in session, and verify it.

SuiteCommerce API reference (NetSuite SuiteCommerce Advanced 2025.1, B2B site). The backend is the only
caller; the mobile app never talks to SuiteCommerce directly. Calls marked VERIFIED were tested against
the sandbox site; everything else is the standard SuiteCommerce behaviour and must be treated as unverified.

Configuration (environment variables, never hard-coded):
- SUITECOMMERCE_BASE_URL: site origin, e.g. https://stanpro2.folio3.site (staging: the mock server).
- SUITECOMMERCE_COMPANY_ID: the `c` query value (sandbox: 628731_SB2). SUITECOMMERCE_SITE_ID: the `n` value (2).
- SUITECOMMERCE_CATALOG_FILTER: value of custitem_f3_acg_selling_sub_multi that limits the catalog to what
  the site sells (sandbox: "Ascot : SG : Standard Products"). Items outside it can fail to add to the cart.
- Country CA, currency CAD, language en, price level 5 (also configuration, not code).

Every request:
- Header `X-Requested-With: XMLHttpRequest` is REQUIRED on service calls. Without it login fails with
  errorCode ERR_INVALID_ORIGIN and no session is created. Send `Accept: application/json`.
- Session = cookies (JSESSIONID, jsid_own, NLShopperId2, chrole, NS_VER, ...). Keep every Set-Cookie from
  login and send them all back on later calls for that user. Inactive sessions expire after about 20 minutes.
- Errors can arrive with HTTP 200. Always check the body: {"errorStatusCode":"400","errorCode":"...",
  "errorMessage":"..."}. Map errorStatusCode to the HTTP status you return; keep errorMessage for the user
  when it is a business rule (e.g. "The minimum quantity for this item is 3.").

Login (VERIFIED):
- POST {base}/{app}/services/Account.Login.Service.ss?n={n}&c={c}
  JSON body {"email": "...", "password": "...", "redirect": "true"}.
- GET instead of POST returns ERR_METHOD_NOT_ALLOWED. Wrong credentials return an error body.
- Response {"user": {...}, "touchpoints": {...}}. Useful user fields: isLoggedIn "T", internalid (customer
  id, string), email, companyname, subsidiary (string id), priceLevel, currency {internalid, code, symbol,
  precision}, paymentterms {internalid, name} (null = cannot check out on terms), custentity_f3_role (role
  id), custentity_f3_disallow_cart (true = may not use the cart), addressbook[] (internalid, addressee,
  addr1, addr2, city, state, zip, country, phone, defaultshipping, defaultbilling).
- Session check: GET {base}/{app}/services/Profile.Service.ss?c={c}&n={n} returns isLoggedIn "T"/"F".
- An expired or missing session is NOT an error on the cart (VERIFIED): the cart read returns an empty guest
  cart with HTTP 200, and pricing returns success false. So check the session (Profile isLoggedIn) before
  cart and checkout calls, or after an unexpectedly empty cart, and return 401 SESSION_EXPIRED when it is "F".
- Logout: GET {base}/{app}/logOut.ssp?logoff=T&ckabandon=T with the session cookies.

Item list and search (VERIFIED, works with or without login):
- GET {base}/api/personalized/items?c={c}&n={n}&country=CA&currency=CAD&language=en&pricelevel=5&use_pcv=F
  &fieldset=search&include=facets&limit=24&offset=0&custitem_f3_acg_selling_sub_multi={catalog filter}
  Optional q={keyword}. Only sort=relevance:desc works; other sorts return 400 "cannot sort by field".
- Response {total, items[], facets[]}. Item fields: internalid (number), itemid (SKU), displayname,
  storedisplayname2, storedescription, urlcomponent, isinstock, ispurchasable, minimumquantity,
  onlinecustomerprice, onlinecustomerprice_formatted, itemimages_detail.urls[] {url, altimagetext}.
- The list price here is the public price, NOT the customer's price (see Pricing).

Item detail (VERIFIED):
- Same endpoint with fieldset=details and id={internalid} (or id=1,2,3 for several), or url={urlcomponent}.
- Extra fields: storedetaileddescription (HTML: sanitise or strip before display), itemtype,
  quantityavailable_detail {quantityavailable, locations[] {internalid, quantityavailable}},
  onlinecustomerprice_detail {onlinecustomerprice, onlinecustomerprice_formatted}, itemoptions_detail.
- Images are absolute URLs on the site; some items have none (show a placeholder).

Customer pricing (VERIFIED, needs a logged-in session):
- GET {base}/{app}_ss2/extensions/Folio3/ItemPricingModule/1.0.0/Modules/ItemPricingModule/SuiteScript2/
  ItemPricingModule.Service.ss?action=getPricingForItems&c={c}&n={n}&customer={user.internalid}
  &subsidiaryId={user.subsidiary}&currencyId={user.currency.internalid}&f3RoleId={user.custentity_f3_role}
  &effectiveDate=&source=api&items=[{"id":197,"quantity":5,"uom":""}]   (items is URL-encoded JSON)
- Response {success, message, data[] {itemId, applicablePrice {price, sourceType, priceLevel, quantity}}}.
  Example: item 197 public price 1613.00, this customer's price 500.00 (sourceType LIQUIDATION).
- success false or a missing item = price unknown. The website then hides the price and BLOCKS checkout
  (fail closed). Do the same: never show the public price as if it were the customer's price.

Cart (VERIFIED). The cart lives in NetSuite per customer and is shared with the website:
- Read: GET {base}/{app}/services/LiveOrder.Service.ss?c={c}&n={n}&internalid=cart
  Response: lines[] {internalid (line id, e.g. "item197set297"), quantity, rate, rate_formatted, amount,
  amount_formatted, item {internalid, itemid, displayname, storedisplayname2, minimumquantity,
  maximumquantity, custitem_f3_incremental_quantity (quantity step), isinstock, itemimages_detail}},
  summary {itemcount, subtotal, subtotal_formatted, shippingcost, taxtotal, total, total_formatted},
  addresses[], shipaddress, billaddress, shipmethod, shipmethods[], paymentmethods[], options
  {custbody_f3_so_shipdate, custbody_f3_so_notes}. Cart rates already include the customer's pricing.
- Add: POST {base}/{app}/services/LiveOrder.Line.Service.ss?c={c}&n={n}
  body [{"item":{"internalid":197},"quantity":5,"options":[],"location":"","fulfillmentChoice":"ship",
  "freeGift":false}]. Adding an item already in the cart increases that line. Returns the whole cart.
- Update quantity: PUT {base}/{app}/services/LiveOrder.Line.Service.ss?c={c}&n={n}&internalid={line id}
  body {"item":{"internalid":197},"quantity":5,"internalid":"item197set297","options":[],"location":"",
  "fulfillmentChoice":"ship","freeGift":false}. Returns the whole cart.
- Remove: DELETE {base}/{app}/services/LiveOrder.Line.Service.ss?c={c}&n={n}&internalid={line id}.
  Returns the whole cart.
- Business errors seen: "The minimum quantity for this item is 3." (below minimumquantity);
  "Invalid item reference key 149 for subsidiary 1." (item not sold to this customer's subsidiary).
  Quantities must be >= minimumquantity and a multiple of custitem_f3_incremental_quantity when set.

Checkout: see suitecommerce_checkout.md (verified against this site). The standard SuiteCommerce flow
does not work here. Payment is on account terms (invoice) with an optional purchase order number; this site
hides card payment, so never collect card data in the app. Order submission stays behind the
CHECKOUT_SUBMIT_ENABLED environment flag (staging with the mock: true; production: false until a human has
verified a submitted order on the site); when it is off, the submit endpoint returns 503
CHECKOUT_SUBMIT_DISABLED with a clear message and the app shows it.

Not in scope for the first release: the agent "select customer" pricing view, quotes (RFQ/CPQ), saved
lists, order history, returns, invoices.
