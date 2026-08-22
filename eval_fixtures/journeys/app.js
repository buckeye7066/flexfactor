#!/usr/bin/env node
/* FlexFactor journey-engine fixture app: plain Node http server, no dependencies.
 * Usage: node app.js [port]   (port 0 = ephemeral; prints "FIXTURE_LISTENING=<port>")
 *
 * Routes:
 *   GET  /              links to every page
 *   GET  /login         login form;  POST /login sets cookie role=admin for admin/admin (303 -> /admin), else 401
 *   GET  /admin         403 unless cookie role=admin
 *   GET  /contact       contact form; POST /contact validates email, stores in memory, 409 on duplicate email
 *   GET  /contact/list  JSON {submissions, received, deletes} (backend state for tests)
 *   GET  /delete-all    page with a destructive POST form; POST /delete-all wipes submissions
 *   GET  /wide          page with a 1400px-wide table (overflows at 390px)
 *   GET  /hang-index    NOT linked from /: links to /hang (a route that never responds) - watchdog test
 *   GET  /hang          never responds (socket held open until the server exits)
 */
'use strict';
const http = require('http');
const { URL } = require('url');
const querystring = require('querystring');

const state = { submissions: [], received: [], deletes: 0 };

function page(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>
<nav><a href="/">Home</a> | <a href="/login">Login</a> | <a href="/admin">Admin</a> | <a href="/contact">Contact</a> | <a href="/contact/list">Submissions</a> | <a href="/delete-all">Danger zone</a> | <a href="/wide">Wide table</a></nav>
<main>${body}</main></body></html>`;
}

function send(res, status, body, headers = {}) {
  const isJson = typeof body !== 'string';
  const payload = isJson ? JSON.stringify(body) : body;
  res.writeHead(status, { 'Content-Type': isJson ? 'application/json' : 'text/html; charset=utf-8', ...headers });
  res.end(payload);
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.cookie || '').split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k) out[k] = decodeURIComponent(v.join('='));
  }
  return out;
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 200000) req.destroy(); });
    req.on('end', () => resolve(querystring.parse(data)));
    req.on('error', () => resolve({}));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const p = url.pathname;
  const isAdmin = cookies(req).role === 'admin';

  if (req.method === 'GET' && p === '/') {
    return send(res, 200, page('Fixture home', `<h1>Journey fixture</h1><p>${isAdmin ? 'Logged in as admin' : 'Anonymous'}</p>
<button type="button" id="toggle" onclick="document.getElementById('toggled').textContent='toggled'">Toggle panel</button><span id="toggled"></span>`));
  }
  if (p === '/login') {
    if (req.method === 'GET') {
      return send(res, 200, page('Login', `<h1>Login</h1><form method="post" action="/login">
<label for="user">User</label><input id="user" name="user" type="text" required>
<label for="pass">Password</label><input id="pass" name="pass" type="password" required>
<button type="submit">Log in</button></form>`));
    }
    const body = await readBody(req);
    if (body.user === 'admin' && body.pass === 'admin') {
      return send(res, 303, '', { 'Set-Cookie': 'role=admin; Path=/; HttpOnly', Location: '/admin' });
    }
    return send(res, 401, page('Login failed', '<h1>Login</h1><p role="alert">Invalid credentials</p><a href="/login">Try again</a>'));
  }
  if (p === '/admin' && req.method === 'GET') {
    if (!isAdmin) return send(res, 403, page('Forbidden', '<h1>Forbidden</h1><p>Admin only.</p>'));
    return send(res, 200, page('Admin', `<h1>Admin area</h1><p>Welcome, admin. ${state.submissions.length} submissions stored.</p>`));
  }
  if (p === '/contact') {
    if (req.method === 'GET') {
      return send(res, 200, page('Contact', `<h1>Contact</h1><form method="post" action="/contact">
<label for="name">Name</label><input id="name" name="name" type="text" required>
<label for="email">Email</label><input id="email" name="email" type="email" required>
<label for="message">Message</label><textarea id="message" name="message"></textarea>
<button type="submit">Submit</button></form>`));
    }
    const body = await readBody(req);
    const rec = { name: String(body.name || ''), email: String(body.email || ''), message: String(body.message || ''), at: Date.now() };
    state.received.push({ ...rec, messageLength: rec.message.length, message: rec.message.slice(0, 80) });
    if (!rec.name || !rec.email) return send(res, 400, page('Error', '<h1>Contact</h1><p role="alert">Missing required fields</p>'));
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(rec.email)) return send(res, 400, page('Error', '<h1>Contact</h1><p role="alert">Invalid email</p>'));
    if (rec.message.length > 5000) return send(res, 413, page('Error', '<h1>Contact</h1><p role="alert">Message too long</p>'));
    if (state.submissions.some((s) => s.email === rec.email)) return send(res, 409, page('Duplicate', '<h1>Contact</h1><p role="alert">Duplicate submission</p>'));
    state.submissions.push(rec);
    return send(res, 201, page('Thanks', `<h1>Thanks</h1><p>Received from ${rec.email}</p>`));
  }
  if (p === '/contact/list' && req.method === 'GET') return send(res, 200, state);
  if (p === '/delete-all') {
    if (req.method === 'GET') {
      return send(res, 200, page('Danger zone', `<h1>Danger zone</h1><form method="post" action="/delete-all"><button type="submit">Delete all submissions</button></form>`));
    }
    state.deletes++; state.submissions = [];
    return send(res, 200, page('Deleted', '<h1>All submissions deleted</h1>'));
  }
  if (p === '/hang-index' && req.method === 'GET') {
    return send(res, 200, page('Hang index', '<h1>Hang index</h1><p><a href="/hang">Hanging route</a></p>'));
  }
  if (p === '/hang') {
    state.hanging = (state.hanging || 0) + 1;
    req.socket.setTimeout(0);
    return; // deliberately never responds
  }
  if (p === '/wide' && req.method === 'GET') {
    const cells = Array.from({ length: 14 }, (_, i) => `<td>col ${i + 1}</td>`).join('');
    return send(res, 200, page('Wide table', `<h1>Wide table</h1><table style="width:1400px;min-width:1400px;table-layout:fixed"><tr>${cells}</tr></table>`));
  }
  return send(res, 404, page('Not found', '<h1>Not found</h1>'));
});

const port = Number(process.argv[2] || process.env.PORT || 0);
server.listen(port, '127.0.0.1', () => {
  console.log(`FIXTURE_LISTENING=${server.address().port}`);
});
