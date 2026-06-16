# Third-Party Licenses — inject-scripts/

This directory contains helper scripts adapted from the open-source project
[mcp-chrome](https://github.com/hangwin/mcp-chrome), distributed under the MIT License.

## accessibility-tree-helper.js / click-helper.js / dom-observer.js

- **Source**: https://github.com/hangwin/mcp-chrome (path: `app/chrome-extension/inject-scripts/`)
- **License**: MIT
- **Copyright**: (c) 2024 hangye

```
MIT License

Copyright (c) 2024 hangye

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

These files are bundled into the extension via `chrome.scripting.executeScript({ files: [...] })`
to bypass page CSP restrictions on `eval`/inline code, while exposing a standardized message API
on the page (`generateAccessibilityTree`, `clickElement`, `set_dom_triggers`).
