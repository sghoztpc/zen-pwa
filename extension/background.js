const NATIVE_HOST = "zen_pwa_host";
const POPUP_URL = browser.runtime.getURL("popup.html");

function isTrustedPopupSender(sender) {
  return Boolean(
    sender &&
    sender.id === browser.runtime.id &&
    !sender.tab &&
    sender.url === POPUP_URL
  );
}

function isSameOriginHttpUrl(urlStr, expectedOrigin) {
  try {
    const u = new URL(urlStr);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
    return expectedOrigin ? u.origin === expectedOrigin : true;
  } catch {
    return false;
  }
}

async function sendNative(payload) {
  try {
    const resp = await browser.runtime.sendNativeMessage(NATIVE_HOST, payload);
    return resp || { ok: false, error: "Empty response from native host." };
  } catch (err) {
    return {
      ok: false,
      error: err && err.message ? err.message : String(err),
      hostDisconnected: true
    };
  }
}

async function getInstalledApps() {
  const nativeResp = await sendNative({ action: "list" });
  if (nativeResp.ok && Array.isArray(nativeResp.apps)) {
    await browser.storage.local.set({ installedApps: nativeResp.apps });
    return { ok: true, apps: nativeResp.apps, nativeConnected: true };
  }
  const stored = await browser.storage.local.get("installedApps");
  return {
    ok: false,
    apps: Array.isArray(stored.installedApps) ? stored.installedApps : [],
    nativeConnected: false,
    error: nativeResp.error
  };
}

async function updateBadge(tabId, hasManifest, origin) {
  try {
    const stored = await browser.storage.local.get("installedApps");
    const apps = Array.isArray(stored.installedApps) ? stored.installedApps : [];
    const isInstalled = apps.some((a) => a.origin === origin);

    if (isInstalled) {
      await browser.browserAction.setBadgeText({ tabId, text: "✓" });
      await browser.browserAction.setBadgeBackgroundColor({ tabId, color: "#16a34a" });
    } else if (hasManifest) {
      await browser.browserAction.setBadgeText({ tabId, text: "+" });
      await browser.browserAction.setBadgeBackgroundColor({ tabId, color: "#2563eb" });
    } else {
      await browser.browserAction.setBadgeText({ tabId, text: "" });
    }
  } catch {
    // Tab may have closed.
  }
}

async function refreshBadgesForOrigin(origin) {
  if (!origin) return;
  try {
    const tabs = await browser.tabs.query({});
    for (const tab of tabs) {
      if (!tab.url || tab.id == null) continue;
      try {
        if (new URL(tab.url).origin === origin) {
          await updateBadge(tab.id, false, origin);
        }
      } catch {
        // Ignore non-standard tab URLs.
      }
    }
  } catch {
    // Ignore tab query errors.
  }
}

browser.runtime.onMessage.addListener((msg, sender) => {
  if (!msg || !msg.type) return undefined;

  if (msg.type === "PAGE_PWA_STATUS" && sender.tab && sender.tab.id != null) {
    updateBadge(sender.tab.id, Boolean(msg.hasManifest), msg.origin || "");
    return undefined;
  }

  if (!isTrustedPopupSender(sender)) {
    return Promise.resolve({ ok: false, error: "Unauthorized message sender." });
  }

  if (msg.type === "NATIVE_PING") {
    return sendNative({ action: "ping" });
  }

  if (msg.type === "LIST_APPS") {
    return getInstalledApps();
  }

  if (msg.type === "INSTALL_APP") {
    return (async () => {
      const resp = await sendNative({
        action: "install",
        name: msg.name,
        startUrl: msg.startUrl,
        origin: msg.origin,
        iconUrl: msg.iconUrl,
        themeColor: msg.themeColor,
        createDesktopShortcut: Boolean(msg.createDesktopShortcut),
        syncSession: msg.syncSession !== false,
        launchAfterInstall: Boolean(msg.launchAfterInstall)
      });
      if (resp.ok) {
        await getInstalledApps();
        await refreshBadgesForOrigin(resp.app && resp.app.origin);
      }
      return resp;
    })();
  }

  if (msg.type === "LAUNCH_APP") {
    return sendNative({ action: "launch", id: msg.id });
  }

  if (msg.type === "UNINSTALL_APP") {
    return (async () => {
      const prev = await browser.storage.local.get("installedApps");
      const prevApps = Array.isArray(prev.installedApps) ? prev.installedApps : [];
      const removed = prevApps.find((a) => a.id === msg.id);
      const resp = await sendNative({ action: "uninstall", id: msg.id });
      if (resp.ok) {
        await getInstalledApps();
        if (removed && removed.origin) {
          await refreshBadgesForOrigin(removed.origin);
        }
      }
      return resp;
    })();
  }

  if (msg.type === "OPEN_POPUP_WINDOW") {
    if (!isSameOriginHttpUrl(msg.url, msg.origin)) {
      return Promise.resolve({ ok: false, error: "Invalid or cross-origin URL." });
    }
    return browser.windows.create({
      url: msg.url,
      type: "popup",
      width: 1200,
      height: 800
    });
  }

  return undefined;
});
