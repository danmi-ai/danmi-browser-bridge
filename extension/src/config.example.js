// Browser Bridge — extension config (TEMPLATE).
//
// Copy this file to `config.js` and adjust. `config.js` is gitignored.
//   cp src/config.example.js src/config.js
// (The GitHub Actions build seeds config.js from this file automatically.)
//
// DISCOVERY_URL: OPTIONAL. A static JSON ({"server_url": "http://host:port"})
//   the extension polls to self-heal when the server's IP changes. Self-hosters
//   usually have a stable address — leave this empty and just type the server
//   URL into the popup once. Set it only if you host your own discovery anchor.
// HOMEPAGE_URL: opened from the popup logo.
export const DISCOVERY_URL = "";
export const HOMEPAGE_URL =
  "https://github.com/danmi-ai/danmi-browser-bridge";

