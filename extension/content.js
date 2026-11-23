(() => {
  function resolveUrl(href, base) {
    if (!href) return null;
    try {
      return new URL(href, base).href;
    } catch {
      return null;
    }
  }

  function isSameOriginHttp(urlStr) {
    try {
      const u = new URL(urlStr);
      return (u.protocol === "http:" || u.protocol === "https:") && u.origin === location.origin;
    } catch {
      return false;
    }
  }

  function isSvgUrl(urlStr, typeStr) {
    if ((typeStr || "").toLowerCase().includes("svg")) return true;
    try {
      return new URL(urlStr).pathname.toLowerCase().endsWith(".svg");
    } catch {
      return false;
    }
  }

  function parseIconSize(sizesStr) {
    if (!sizesStr) return 0;
    let best = 0;
    for (const part of sizesStr.trim().split(/\s+/)) {
      if (part.toLowerCase() === "any") {
        best = Math.max(best, 512);
        continue;
      }
      const m = /^(\d+)x(\d+)$/i.exec(part);
      if (m) {
        best = Math.max(best, Math.min(parseInt(m[1], 10), parseInt(m[2], 10)));
      }
    }
    return best;
  }

  function pickBestIcon(icons, baseUrl) {
    if (!Array.isArray(icons) || icons.length === 0) return null;
    const candidates = icons
      .map((ic) => {
        const src = resolveUrl(ic.src, baseUrl);
        if (!src) return null;
        const size = parseIconSize(ic.sizes);
        const type = (ic.type || "").toLowerCase();
        const isSvg = isSvgUrl(src, type);
        const isPng = type.includes("png") || new URL(src).pathname.toLowerCase().endsWith(".png");
        const score = (isSvg ? -1000 : 0) + (isPng ? 200 : 0) + Math.min(size, 512);
        return { src, size, score };
      })
      .filter(Boolean)
      .sort((a, b) => b.score - a.score);

    return candidates.length > 0 ? candidates[0].src : null;
  }

  function findFallbackIcon() {
    const selectors = [
      'link[rel="apple-touch-icon"]',
      'link[rel="apple-touch-icon-precomposed"]',
      'link[rel="icon"][sizes]',
      'link[rel="shortcut icon"]',
      'link[rel="icon"]'
    ];
    let best = null;
    let bestScore = -9999;

    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        const href = el.getAttribute("href");
        const url = resolveUrl(href, location.href);
        if (!url) continue;
        const sizes = el.getAttribute("sizes") || "";
        const size = parseIconSize(sizes);
        const isSvg = isSvgUrl(url, el.getAttribute("type"));
        const isApple = (el.getAttribute("rel") || "").includes("apple-touch");
        const score = (isSvg ? -1000 : 0) + (isApple ? 300 : 0) + size;
        if (score > bestScore) {
          bestScore = score;
          best = url;
        }
      }
    }
    return best || resolveUrl("/favicon.ico", location.origin);
  }

  async function fetchManifest(manifestUrl) {
    if (!manifestUrl || !isSameOriginHttp(manifestUrl)) return null;
    try {
      const resp = await fetch(manifestUrl, { credentials: "same-origin" });
      if (!resp.ok) return null;
      const data = await resp.json();
      return data && typeof data === "object" ? data : null;
    } catch {
      return null;
    }
  }

  async function detectPageAppInfo() {
    const manifestLink = document.querySelector('link[rel="manifest"]');
    const manifestUrl = manifestLink ? resolveUrl(manifestLink.getAttribute("href"), location.href) : null;
    const manifest = await fetchManifest(manifestUrl);

    const name =
      (manifest && (manifest.short_name || manifest.name)) ||
      document.querySelector('meta[name="application-name"]')?.getAttribute("content") ||
      document.title ||
      location.hostname;

    const rawStart = manifest && manifest.start_url
      ? resolveUrl(manifest.start_url, manifestUrl || location.href)
      : null;
    const startUrl = rawStart && isSameOriginHttp(rawStart) ? rawStart : location.href;

    const iconUrl =
      (manifest && pickBestIcon(manifest.icons, manifestUrl || location.href)) ||
      findFallbackIcon();

    const themeColor =
      (manifest && manifest.theme_color) ||
      document.querySelector('meta[name="theme-color"]')?.getAttribute("content") ||
      "#1f1f1f";

    return {
      hasManifest: Boolean(manifest),
      manifestUrl,
      name: String(name).trim().slice(0, 80),
      startUrl,
      currentUrl: location.href,
      origin: location.origin,
      iconUrl,
      themeColor,
      display: (manifest && manifest.display) || "standalone"
    };
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "GET_PAGE_APP_INFO") {
      return detectPageAppInfo();
    }
    return undefined;
  });

  const hasManifestLink = Boolean(document.querySelector('link[rel="manifest"]'));
  browser.runtime.sendMessage({
    type: "PAGE_PWA_STATUS",
    hasManifest: hasManifestLink,
    origin: location.origin
  }).catch(() => {});
})();
