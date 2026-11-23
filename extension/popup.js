let currentPageInfo = null;
let nativeReady = false;
let installedApps = [];

const hostStatusEl = document.getElementById("hostStatus");
const siteIconEl = document.getElementById("siteIcon");
const manifestBadgeEl = document.getElementById("manifestBadge");
const appNameInputEl = document.getElementById("appNameInput");
const startUrlInputEl = document.getElementById("startUrlInput");
const syncSessionEl = document.getElementById("syncSessionCheck");
const desktopShortcutEl = document.getElementById("desktopShortcutCheck");
const installBtnEl = document.getElementById("installBtn");
const popupWindowBtnEl = document.getElementById("popupWindowBtn");
const messageBoxEl = document.getElementById("messageBox");
const appsListEl = document.getElementById("appsList");

function showMessage(text, isError = false) {
  messageBoxEl.textContent = text;
  messageBoxEl.className = "message " + (isError ? "err" : "ok");
}

function clearMessage() {
  messageBoxEl.className = "message hidden";
  messageBoxEl.textContent = "";
}

function isSafePreviewIconUrl(iconUrl, pageOrigin) {
  if (!iconUrl) return false;
  try {
    const u = new URL(iconUrl);
    return u.protocol === "https:" || (u.protocol === "http:" && u.origin === pageOrigin);
  } catch {
    return false;
  }
}

function getValidatedStartUrl() {
  if (!currentPageInfo) return null;
  const raw = startUrlInputEl.value.trim() || currentPageInfo.startUrl;
  try {
    const u = new URL(raw);
    if ((u.protocol !== "http:" && u.protocol !== "https:") || u.origin !== currentPageInfo.origin) {
      return null;
    }
    return u.href;
  } catch {
    return null;
  }
}

function refreshInstallButtonLabel() {
  if (!currentPageInfo) return;
  const alreadyInstalled = installedApps.some((a) => a.origin === currentPageInfo.origin);
  installBtnEl.textContent = alreadyInstalled ? "Update / Reinstall App" : "Install as Zen App";
}

async function checkHost() {
  try {
    const ping = await browser.runtime.sendMessage({ type: "NATIVE_PING" });
    if (ping && ping.ok) {
      nativeReady = true;
      hostStatusEl.textContent = `Helper v${ping.version || "1"} ready`;
      hostStatusEl.className = "status-pill status-ok";
      return;
    }
  } catch {
    // Fall through to offline state.
  }
  nativeReady = false;
  hostStatusEl.textContent = "Helper offline";
  hostStatusEl.className = "status-pill status-err";
  showMessage(
    "Windows helper not connected. Run register_host.ps1 once, or use Popup Window.",
    true
  );
}

async function loadCurrentTab() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab || !tab.url || !/^https?:\/\//i.test(tab.url)) {
    manifestBadgeEl.textContent = "Open any website";
    manifestBadgeEl.className = "badge badge-fallback";
    appNameInputEl.value = "";
    appNameInputEl.disabled = true;
    startUrlInputEl.value = "";
    startUrlInputEl.disabled = true;
    installBtnEl.disabled = true;
    popupWindowBtnEl.disabled = true;
    return;
  }

  let info = null;
  try {
    info = await browser.tabs.sendMessage(tab.id, { type: "GET_PAGE_APP_INFO" });
  } catch {
    info = null;
  }

  const fallbackOrigin = new URL(tab.url).origin;
  currentPageInfo = info && typeof info === "object" ? info : {
    hasManifest: false,
    name: (tab.title || new URL(tab.url).hostname).slice(0, 80),
    startUrl: tab.url,
    currentUrl: tab.url,
    origin: fallbackOrigin,
    iconUrl: tab.favIconUrl || "",
    themeColor: "#1f1f1f"
  };

  if (isSafePreviewIconUrl(currentPageInfo.iconUrl, currentPageInfo.origin)) {
    siteIconEl.src = currentPageInfo.iconUrl;
  }
  manifestBadgeEl.textContent = currentPageInfo.hasManifest
    ? "PWA Manifest detected"
    : "Any Website Mode";
  manifestBadgeEl.className = currentPageInfo.hasManifest ? "badge" : "badge badge-fallback";
  appNameInputEl.value = currentPageInfo.name || "";
  startUrlInputEl.value = currentPageInfo.startUrl || tab.url;
  installBtnEl.disabled = !nativeReady;
  popupWindowBtnEl.disabled = false;
  refreshInstallButtonLabel();
}

function createAppRow(app) {
  const row = document.createElement("div");
  row.className = "app-item";

  const info = document.createElement("div");
  info.className = "app-info";

  const nameEl = document.createElement("div");
  nameEl.className = "app-name";
  nameEl.textContent = app.name;

  const urlEl = document.createElement("div");
  urlEl.className = "app-url";
  urlEl.textContent = app.startUrl;

  info.appendChild(nameEl);
  info.appendChild(urlEl);

  const actions = document.createElement("div");
  actions.className = "app-actions";

  const launchBtn = document.createElement("button");
  launchBtn.className = "btn btn-secondary btn-small";
  launchBtn.textContent = "Launch";
  launchBtn.addEventListener("click", async () => {
    try {
      const r = await browser.runtime.sendMessage({ type: "LAUNCH_APP", id: app.id });
      if (!r || !r.ok) {
        showMessage((r && r.error) || "Failed to launch app.", true);
      }
    } catch (err) {
      showMessage(err && err.message ? err.message : "Failed to launch app.", true);
    }
  });

  const delBtn = document.createElement("button");
  delBtn.className = "btn btn-danger btn-small";
  delBtn.textContent = "Remove";
  delBtn.addEventListener("click", async () => {
    delBtn.disabled = true;
    try {
      const r = await browser.runtime.sendMessage({ type: "UNINSTALL_APP", id: app.id });
      if (r && r.ok) {
        showMessage(`Removed "${app.name}".`);
        await renderInstalledApps();
      } else {
        showMessage((r && r.error) || "Failed to remove app.", true);
        delBtn.disabled = false;
      }
    } catch (err) {
      showMessage(err && err.message ? err.message : "Failed to remove app.", true);
      delBtn.disabled = false;
    }
  });

  actions.appendChild(launchBtn);
  actions.appendChild(delBtn);
  row.appendChild(info);
  row.appendChild(actions);
  return row;
}

async function renderInstalledApps() {
  try {
    const res = await browser.runtime.sendMessage({ type: "LIST_APPS" });
    installedApps = (res && res.apps) || [];
  } catch {
    installedApps = [];
  }

  refreshInstallButtonLabel();
  appsListEl.textContent = "";
  if (installedApps.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No apps installed yet.";
    appsListEl.appendChild(empty);
    return;
  }

  for (const app of installedApps) {
    appsListEl.appendChild(createAppRow(app));
  }
}

installBtnEl.addEventListener("click", async () => {
  if (!currentPageInfo || !nativeReady) return;
  const appName = appNameInputEl.value.trim() || currentPageInfo.name;
  const startUrl = getValidatedStartUrl();
  if (!appName) {
    showMessage("Enter an app name first.", true);
    return;
  }
  if (!startUrl) {
    showMessage(`Start URL must be on ${currentPageInfo.origin}.`, true);
    return;
  }

  clearMessage();
  installBtnEl.disabled = true;
  installBtnEl.textContent = "Installing...";

  try {
    const res = await browser.runtime.sendMessage({
      type: "INSTALL_APP",
      name: appName,
      startUrl,
      origin: currentPageInfo.origin,
      iconUrl: currentPageInfo.iconUrl,
      themeColor: currentPageInfo.themeColor,
      createDesktopShortcut: desktopShortcutEl.checked,
      syncSession: syncSessionEl.checked,
      launchAfterInstall: true
    });

    if (res && res.ok) {
      const cookieNote = res.cookieWarning
        ? ` (${res.cookieWarning})`
        : res.cookiesSynced > 0
          ? ` Synced ${res.cookiesSynced} login cookies.`
          : "";
      showMessage(`Installed "${res.app.name}"!${cookieNote}`);
      await renderInstalledApps();
    } else {
      showMessage((res && res.error) || "Install failed.", true);
    }
  } catch (err) {
    showMessage(err && err.message ? err.message : "Install failed.", true);
  } finally {
    installBtnEl.disabled = !nativeReady;
    refreshInstallButtonLabel();
  }
});

popupWindowBtnEl.addEventListener("click", async () => {
  if (!currentPageInfo) return;
  const startUrl = getValidatedStartUrl() || currentPageInfo.startUrl;
  try {
    const res = await browser.runtime.sendMessage({
      type: "OPEN_POPUP_WINDOW",
      url: startUrl,
      origin: currentPageInfo.origin
    });
    if (res && res.ok === false) {
      showMessage(res.error || "Failed to open popup window.", true);
      return;
    }
    window.close();
  } catch (err) {
    showMessage(err && err.message ? err.message : "Failed to open popup window.", true);
  }
});

(async () => {
  try {
    await checkHost();
    await loadCurrentTab();
    await renderInstalledApps();
  } catch (err) {
    showMessage(err && err.message ? err.message : "Initialization error.", true);
  }
})();
