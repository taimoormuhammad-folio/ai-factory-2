// Mock of the NetSuite SuiteCommerce services the backend calls. Staging and device tests only: it lets the
// pipeline run the app end to end without the real site. Shapes follow the real responses (see
// fixtures/ and docs in README.md). No dependencies: node server.js (Node 18+).
'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const PORT = Number(process.env.PORT || 8080);
const SESSION_TTL_MS = Number(process.env.MOCK_SESSION_TTL_S || 1200) * 1000;
const COMPANY_ID = process.env.MOCK_COMPANY_ID || '628731_SB2';

const fixtures = (name) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', name), 'utf8'));
const ITEMS = fixtures('items.json').items;
const CUSTOMERS = fixtures('customer.json');

const sessions = new Map(); // session id -> { user, lastSeen }
const carts = new Map();    // customer internalid -> { lines, shipaddress, billaddress, shipmethod, paymentmethods, options, nextLine }
let nextOrder = 1000;

const money = (n) => new Intl.NumberFormat('en-CA', { style: 'currency', currency: 'CAD' }).format(n);
const round2 = (n) => Math.round(n * 100) / 100;

function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'Content-Type': 'application/json;charset=utf-8', ...headers });
  res.end(JSON.stringify(body));
}

// The real site answers some errors with HTTP 200 and the error in the body.
function error(res, statusCode, code, message, httpStatus = statusCode) {
  send(res, httpStatus, { errorStatusCode: String(statusCode), errorCode: code, errorMessage: message });
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

function currentUser(req) {
  const id = cookies(req).JSESSIONID;
  const s = id && sessions.get(id);
  if (!s) return null;
  if (Date.now() - s.lastSeen > SESSION_TTL_MS) {
    sessions.delete(id);
    return null;
  }
  s.lastSeen = Date.now();
  return s.user;
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch { resolve(undefined); }
    });
  });
}

function itemById(id) {
  return ITEMS.find((i) => String(i.internalid) === String(id));
}

function publicItem(item, fieldset) {
  const { _mock, ...rest } = item;
  const price = rest.onlinecustomerprice_detail || {};
  if (fieldset === 'details') return rest;
  return {
    internalid: rest.internalid, itemid: rest.itemid, displayname: rest.displayname,
    storedisplayname2: rest.storedisplayname2, storedescription: rest.storedescription,
    urlcomponent: rest.urlcomponent, isinstock: rest.isinstock, ispurchasable: rest.ispurchasable,
    minimumquantity: rest.minimumquantity, itemimages_detail: rest.itemimages_detail,
    onlinecustomerprice: price.onlinecustomerprice, onlinecustomerprice_formatted: price.onlinecustomerprice_formatted,
  };
}

function cartFor(user) {
  if (!carts.has(user.internalid)) {
    carts.set(user.internalid, { lines: [], shipaddress: null, billaddress: null, shipmethod: null,
      paymentmethods: [], options: { custbody_f3_so_shipdate: '', custbody_f3_so_notes: '' }, nextLine: 300 });
  }
  return carts.get(user.internalid);
}

function cartView(user, cart) {
  const lines = cart.lines.map((l) => {
    const item = itemById(l.itemId);
    const rate = item._mock.customerPrice;
    const amount = round2(rate * l.quantity);
    return {
      internalid: l.id, quantity: l.quantity, rate, rate_formatted: money(rate), amount, amount_formatted: money(amount),
      total: amount, total_formatted: money(amount),
      item: {
        internalid: item.internalid, itemid: item.itemid, displayname: item.displayname,
        storedisplayname2: item.storedisplayname2, minimumquantity: item.minimumquantity, maximumquantity: null,
        custitem_f3_incremental_quantity: item._mock.quantityStep, isinstock: item.isinstock,
        ispurchasable: item.ispurchasable, urlcomponent: item.urlcomponent, itemimages_detail: item.itemimages_detail,
      },
    };
  });
  const subtotal = round2(lines.reduce((s, l) => s + l.amount, 0));
  const method = CUSTOMERS.shipmethods.find((m) => m.internalid === cart.shipmethod);
  const shipping = lines.length && method ? method.rate : 0;
  const tax = round2((subtotal + shipping) * CUSTOMERS.taxRate);
  const total = round2(subtotal + shipping + tax);
  return {
    lines,
    summary: {
      itemcount: lines.reduce((s, l) => s + l.quantity, 0),
      subtotal, subtotal_formatted: money(subtotal),
      shippingcost: shipping, shippingcost_formatted: money(shipping),
      taxtotal: tax, taxtotal_formatted: money(tax),
      total, total_formatted: money(total),
    },
    addresses: user.addressbook,
    shipaddress: cart.shipaddress, billaddress: cart.billaddress, shipmethod: cart.shipmethod,
    shipmethods: cart.shipaddress ? CUSTOMERS.shipmethods : [],
    paymentmethods: cart.paymentmethods,
    options: cart.options,
  };
}

function quantityProblem(item, quantity) {
  if (!Number.isInteger(quantity) || quantity < 1) return 'Quantity must be a whole number of at least 1.';
  if (item.minimumquantity && quantity < item.minimumquantity) {
    return `The minimum quantity for this item is ${item.minimumquantity}.`;
  }
  const step = item._mock.quantityStep;
  if (step && quantity % step !== 0) return `This item is sold in multiples of ${step}.`;
  return null;
}

async function handleLogin(req, res) {
  if (req.method !== 'POST') {
    return error(res, 405, 'ERR_METHOD_NOT_ALLOWED', 'Sorry, you are not allowed to perform this action.', 200);
  }
  if (req.headers['x-requested-with'] !== 'XMLHttpRequest') {
    return error(res, 400, 'ERR_INVALID_ORIGIN', 'Invalid request origin', 200);
  }
  const body = await readBody(req);
  const match = body && CUSTOMERS.users.find((u) => u.email === body.email && u.password === body.password);
  if (!match) return error(res, 401, 'ERR_WS_INVALID_LOGIN', 'Invalid email address or password.');
  const id = crypto.randomBytes(18).toString('base64url');
  sessions.set(id, { user: match.user, lastSeen: Date.now() });
  const shopper = crypto.randomBytes(12).toString('base64url');
  send(res, 200, { user: match.user, touchpoints: { logout: '/store/logOut.ssp?logoff=T&ckabandon=T' } }, {
    'Set-Cookie': [
      `JSESSIONID=${id}; Path=/; HttpOnly; SameSite=Lax`,
      `jsid_own=${COMPANY_ID}; Path=/; HttpOnly`,
      `NLShopperId2=${shopper}; Path=/; HttpOnly`,
    ],
  });
}

async function handleCartLines(req, res, url, user) {
  const cart = cartFor(user);
  if (req.method === 'POST') {
    const body = await readBody(req);
    if (!Array.isArray(body) || !body.length) return error(res, 400, 'ERR_BAD_REQUEST', 'Expected a list of lines.');
    for (const line of body) {
      const item = itemById(line.item && line.item.internalid);
      if (!item || !item._mock.soldToSubsidiary) {
        return error(res, 400, 'INVALID_KEY_OR_REF',
          `Invalid item reference key ${line.item && line.item.internalid} for subsidiary ${user.subsidiary}.`);
      }
      const existing = cart.lines.find((l) => String(l.itemId) === String(item.internalid));
      const quantity = (existing ? existing.quantity : 0) + Number(line.quantity);
      const problem = quantityProblem(item, quantity);
      if (problem) return error(res, 400, 'ERR_INVALID_QUANTITY', problem);
      if (existing) existing.quantity = quantity;
      else cart.lines.push({ id: `item${item.internalid}set${cart.nextLine++}`, itemId: item.internalid, quantity });
    }
    return send(res, 200, cartView(user, cart));
  }
  const lineId = url.searchParams.get('internalid');
  const line = cart.lines.find((l) => l.id === lineId);
  if (!line) return error(res, 404, 'ERR_LINE_NOT_FOUND', `Line ${lineId} is not in the cart.`);
  if (req.method === 'PUT') {
    const body = await readBody(req);
    const problem = quantityProblem(itemById(line.itemId), Number(body && body.quantity));
    if (problem) return error(res, 400, 'ERR_INVALID_QUANTITY', problem);
    line.quantity = Number(body.quantity);
    return send(res, 200, cartView(user, cart));
  }
  if (req.method === 'DELETE') {
    cart.lines = cart.lines.filter((l) => l !== line);
    return send(res, 200, cartView(user, cart));
  }
  return error(res, 405, 'ERR_METHOD_NOT_ALLOWED', 'Sorry, you are not allowed to perform this action.', 200);
}

function applyOrderUpdate(user, cart, body) {
  const ids = new Set(user.addressbook.map((a) => a.internalid));
  if (body.shipaddress !== undefined) {
    if (body.shipaddress && !ids.has(body.shipaddress)) return 'Unknown shipping address.';
    cart.shipaddress = body.shipaddress;
  }
  if (body.billaddress !== undefined) {
    if (body.billaddress && !ids.has(body.billaddress)) return 'Unknown billing address.';
    cart.billaddress = body.billaddress;
  }
  if (body.shipmethod !== undefined) {
    if (body.shipmethod && !CUSTOMERS.shipmethods.some((m) => m.internalid === body.shipmethod)) {
      return 'Unknown shipping method.';
    }
    cart.shipmethod = body.shipmethod;
  }
  if (Array.isArray(body.paymentmethods)) cart.paymentmethods = body.paymentmethods;
  if (body.options) cart.options = { ...cart.options, ...body.options };
  return null;
}

async function handleOrder(req, res, user) {
  const cart = cartFor(user);
  if (req.method === 'GET') return send(res, 200, cartView(user, cart));
  if (req.method !== 'PUT' && req.method !== 'POST') {
    return error(res, 405, 'ERR_METHOD_NOT_ALLOWED', 'Sorry, you are not allowed to perform this action.', 200);
  }
  const body = await readBody(req);
  if (!body || typeof body !== 'object') return error(res, 400, 'ERR_BAD_REQUEST', 'Expected the order as JSON.');
  const problem = applyOrderUpdate(user, cart, body);
  if (problem) return error(res, 400, 'ERR_INVALID_ORDER', problem);
  if (req.method === 'PUT') return send(res, 200, cartView(user, cart));

  // POST = submit the order.
  if (!cart.lines.length) return error(res, 400, 'ERR_EMPTY_CART', 'Your cart is empty.');
  if (!cart.shipaddress || !cart.billaddress) return error(res, 400, 'ERR_MISSING_ADDRESS', 'Choose a shipping and billing address.');
  if (!cart.shipmethod) return error(res, 400, 'ERR_MISSING_SHIPMETHOD', 'Choose a shipping method.');
  if (!user.paymentterms) {
    return error(res, 400, 'ERR_NO_PAYMENT_TERMS', "Customer doesn't have term defined in NetSuite. Please contact support.");
  }
  const pay = cart.paymentmethods.find((p) => p.type === 'invoice');
  if (!pay || !pay.terms || pay.terms.internalid !== user.paymentterms.internalid) {
    return error(res, 400, 'ERR_INVALID_PAYMENT', 'Pay on your account terms (invoice).');
  }
  const view = cartView(user, cart);
  const n = nextOrder++;
  carts.delete(user.internalid);
  return send(res, 200, {
    confirmation: {
      internalid: String(50000 + n), tranid: `SO${n}`, confirmationnumber: `SO${n}`,
      purchasenumber: pay.purchasenumber || '', summary: view.summary,
    },
  });
}

function handleItems(res, url) {
  const p = url.searchParams;
  const sort = p.get('sort');
  if (sort && sort !== 'relevance:desc') {
    return send(res, 400, { errors: `cannot sort by field '${sort}'`, code: 400, warnings: {} });
  }
  const fieldset = p.get('fieldset') || 'search';
  let list = ITEMS;
  if (p.get('id')) {
    const ids = new Set(p.get('id').split(','));
    list = list.filter((i) => ids.has(String(i.internalid)));
  }
  if (p.get('url')) list = list.filter((i) => i.urlcomponent === p.get('url'));
  if (p.get('q')) {
    const q = p.get('q').toLowerCase();
    list = list.filter((i) => [i.itemid, i.displayname, i.storedisplayname2, i.storedescription]
      .some((v) => v && String(v).toLowerCase().includes(q)));
  }
  const offset = Number(p.get('offset') || 0);
  const limit = Number(p.get('limit') || 24);
  send(res, 200, { total: list.length, items: list.slice(offset, offset + limit).map((i) => publicItem(i, fieldset)), facets: [] });
}

function handlePricing(res, url, user) {
  if (!user) return send(res, 200, { success: false, items: [], message: 'Pricing service unavailable' });
  let wanted;
  try { wanted = JSON.parse(url.searchParams.get('items') || '[]'); } catch { wanted = null; }
  if (!Array.isArray(wanted)) return send(res, 200, { success: false, items: [], message: 'Invalid items parameter' });
  const data = wanted.map((w) => itemById(w.id)).filter((i) => i && i._mock.customerPrice != null).map((i) => ({
    itemId: i.internalid,
    applicablePrice: { sourceType: i._mock.priceSource, price: i._mock.customerPrice, currency: '1', quantity: 1 },
  }));
  send(res, 200, { success: true, message: `Pricing calculated successfully for ${data.length} item(s)`, errors: [], data });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const p = url.pathname;
  try {
    if (p === '/health') return send(res, 200, { status: 'ok' });
    if (p.endsWith('/Account.Login.Service.ss')) return await handleLogin(req, res);
    if (p.endsWith('/logOut.ssp')) {
      sessions.delete(cookies(req).JSESSIONID);
      return send(res, 200, { loggedOut: true });
    }
    const user = currentUser(req);
    if (p.endsWith('/Profile.Service.ss')) {
      return send(res, 200, user ? { ...user, isLoggedIn: 'T' } : { isLoggedIn: 'F', isGuest: 'T', email: '', internalid: '0' });
    }
    if (p === '/api/personalized/items' || p === '/api/items') return handleItems(res, url);
    if (p.endsWith('/ItemPricingModule.Service.ss')) return handlePricing(res, url, user);
    if (p.endsWith('/LiveOrder.Line.Service.ss') || p.endsWith('/LiveOrder.Service.ss')) {
      // Like the real site: no session reads as an empty guest cart (HTTP 200). Changing a guest cart is
      // not mocked; it returns an error so a backend that forgot the session check fails loudly.
      if (!user && req.method === 'GET') return send(res, 200, cartView({ addressbook: [] }, { lines: [], options: {} }));
      if (!user) return error(res, 401, 'ERR_USER_NOT_LOGGED_IN', 'You must log in to use the cart.');
      if (p.endsWith('/LiveOrder.Line.Service.ss')) return await handleCartLines(req, res, url, user);
      return await handleOrder(req, res, user);
    }
    error(res, 404, 'ERR_NOT_FOUND', `No mock for ${req.method} ${p}`);
  } catch (e) {
    console.error(e);
    error(res, 500, 'ERR_UNEXPECTED', 'Unexpected error in the SuiteCommerce mock.');
  }
});

server.listen(PORT, () => console.log(`SuiteCommerce mock listening on ${PORT}`));
