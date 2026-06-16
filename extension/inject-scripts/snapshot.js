(() => {
  "use strict";

  const MAX_DEPTH = 15;
  const MAX_NODES = 3000;
  const MAX_TEXT = 200;

  let nodeCount = 0;
  let truncated = false;
  let refCounter = 0;

  // Clear previous refs
  document.querySelectorAll("[data-bb-ref]").forEach((el) => el.removeAttribute("data-bb-ref"));

  // ---------------------------------------------------------------------------
  // Visibility check
  // ---------------------------------------------------------------------------
  function isHidden(el) {
    if (el.getAttribute("aria-hidden") === "true") return true;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return true;
    if (el.offsetWidth === 0 && el.offsetHeight === 0) return true;
    return false;
  }

  // ---------------------------------------------------------------------------
  // Interactive element detection
  // ---------------------------------------------------------------------------
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "checkbox", "radio", "tab",
    "menuitem", "switch", "combobox", "textbox",
  ]);

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return true;
    if (tag === "button" || tag === "input" || tag === "select" || tag === "textarea") return true;

    const role = el.getAttribute("role");
    if (role && INTERACTIVE_ROLES.has(role)) return true;

    const ce = el.getAttribute("contenteditable");
    if (ce === "true" || ce === "") return true;

    const tabindex = el.getAttribute("tabindex");
    if (tabindex !== null && parseInt(tabindex, 10) >= 0) return true;

    return false;
  }

  // ---------------------------------------------------------------------------
  // Implicit role mapping
  // ---------------------------------------------------------------------------
  function getImplicitRole(el) {
    const tag = el.tagName.toLowerCase();

    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";

    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "text" || type === "search" || type === "email" ||
          type === "url" || type === "tel" || type === "password" || type === "") {
        return "textbox";
      }
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "submit" || type === "reset" || type === "button") return "button";
      return "textbox"; // fallback for other input types
    }

    if (tag === "img" && el.hasAttribute("alt")) return "img";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "nav") return "navigation";
    if (tag === "main") return "main";
    if (tag === "ul" || tag === "ol") return "list";
    if (tag === "li") return "listitem";
    if (tag === "form") return "form";

    return null;
  }

  // ---------------------------------------------------------------------------
  // Accessible name computation
  // ---------------------------------------------------------------------------
  function getAccessibleName(el) {
    // 1. aria-label
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) return ariaLabel.trim().slice(0, MAX_TEXT);

    // 2. aria-labelledby
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/).map((id) => {
        const ref = document.getElementById(id);
        return ref ? ref.textContent.trim() : "";
      }).filter(Boolean);
      if (parts.length > 0) return parts.join(" ").slice(0, MAX_TEXT);
    }

    const tag = el.tagName.toLowerCase();

    // 3. For input: associated label via for/id
    if (tag === "input" || tag === "select" || tag === "textarea") {
      const id = el.getAttribute("id");
      if (id) {
        const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
        if (label) return label.textContent.trim().slice(0, MAX_TEXT);
      }
      // Check wrapping label
      const parentLabel = el.closest("label");
      if (parentLabel) {
        const text = parentLabel.textContent.trim();
        if (text) return text.slice(0, MAX_TEXT);
      }
    }

    // 4. alt (for img)
    if (tag === "img") {
      const alt = el.getAttribute("alt");
      if (alt) return alt.trim().slice(0, MAX_TEXT);
    }

    // 5. title attribute
    const title = el.getAttribute("title");
    if (title) return title.trim().slice(0, MAX_TEXT);

    // 6. placeholder (for input/textarea)
    if (tag === "input" || tag === "textarea") {
      const ph = el.getAttribute("placeholder");
      if (ph) return ph.trim().slice(0, MAX_TEXT);
    }

    // 7. textContent for buttons/links/headings
    if (tag === "button" || tag === "a" || /^h[1-6]$/.test(tag) ||
        el.getAttribute("role") === "button" || el.getAttribute("role") === "link") {
      const text = el.textContent.trim();
      if (text) return text.slice(0, MAX_TEXT);
    }

    return "";
  }

  // ---------------------------------------------------------------------------
  // Get element value (for inputs)
  // ---------------------------------------------------------------------------
  function getElementValue(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox" || type === "radio") {
        return el.checked ? "true" : "false";
      }
      return el.value || null;
    }
    if (tag === "textarea") return el.value || null;
    if (tag === "select") return el.value || null;
    const ce = el.getAttribute("contenteditable");
    if (ce === "true" || ce === "") {
      const text = el.textContent.trim();
      return text ? text.slice(0, MAX_TEXT) : null;
    }
    return null;
  }

  // ---------------------------------------------------------------------------
  // DOM walk
  // ---------------------------------------------------------------------------
  function walkNode(el, depth) {
    if (nodeCount >= MAX_NODES) {
      truncated = true;
      return null;
    }

    if (depth > MAX_DEPTH) return null;
    if (!(el instanceof Element)) return null;
    if (isHidden(el)) return null;

    const explicitRole = el.getAttribute("role");
    const implicitRole = getImplicitRole(el);
    const role = explicitRole || implicitRole;
    const interactive = isInteractive(el);

    // Assign ref to interactive elements
    let ref = null;
    if (interactive) {
      refCounter++;
      ref = "e" + refCounter;
      el.setAttribute("data-bb-ref", ref);
    }

    // Process children
    const children = [];
    for (const child of el.children) {
      if (nodeCount >= MAX_NODES) {
        truncated = true;
        break;
      }
      const childNode = walkNode(child, depth + 1);
      if (childNode) children.push(childNode);
    }

    // Decide whether to include this node
    const name = getAccessibleName(el);
    const value = interactive ? getElementValue(el) : null;

    // Skip nodes with no role, no ref, no name, and no interactive descendants
    if (!role && !ref && !name && children.length === 0) {
      return null;
    }

    // If this node has no role and no ref but has children, pass children up
    if (!role && !ref && !name && children.length > 0) {
      // Return children as a group only if multiple, otherwise unwrap
      if (children.length === 1) return children[0];
      // Create a generic group node
      nodeCount++;
      return { role: "group", name: "", ref: null, value: null, children };
    }

    // For text-only leaf nodes with no role/ref, collapse text into parent
    if (!role && !ref && name && children.length === 0) {
      // Return as a text node only if it adds information
      nodeCount++;
      return { role: "text", name, ref: null, value: null, children: [] };
    }

    nodeCount++;
    return {
      role: role || "generic",
      name: name || "",
      ref,
      value,
      children,
    };
  }

  // Walk from body
  const tree = [];
  const body = document.body;
  if (body) {
    for (const child of body.children) {
      if (nodeCount >= MAX_NODES) {
        truncated = true;
        break;
      }
      const node = walkNode(child, 1);
      if (node) tree.push(node);
    }
  }

  return {
    url: location.href,
    title: document.title,
    viewport: { width: window.innerWidth, height: window.innerHeight },
    scrollY: window.scrollY,
    tree,
    truncated,
  };
})();
