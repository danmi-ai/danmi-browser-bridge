// Browser Bridge — extension config (TEMPLATE).
//
// Copy this file to `config.js` and fill in your deployment's endpoints.
// `config.js` is gitignored — never commit real internal URLs.
//
//   cp src/config.example.js src/config.js
//
// DISCOVERY_URL: a static JSON file ({"server_url": "http://host:port"}) the
//   extension polls to self-heal when the server's IP changes. Host it anywhere
//   that stays reachable (object storage, a CDN, a static endpoint).
// HOMEPAGE_URL: the product landing / install page (opened from the popup logo).
export const DISCOVERY_URL =
  "https://your-host.example.com/danmi-browser-bridge/discovery.json";
export const HOMEPAGE_URL =
  "https://your-host.example.com/danmi-browser-bridge/index.html";
