(function () {
  "use strict";

  var MENU_ID = "client-download-menu";
  var WINDOWS_LOGO =
    '<svg viewBox="0 0 48 48" aria-hidden="true">' +
    '<path d="M3 9.7 21.5 7v15.1H3z"></path>' +
    '<path d="M25 6.5 45 3.7v18.4H25z"></path>' +
    '<path d="M3 25.9h18.5V41L3 38.3z"></path>' +
    '<path d="M25 25.9h20v18.4L25 41.5z"></path></svg>';

  var DEFAULTS = {
    mobileUrl: "",
    mobileQrImage: "",
    windowsUrl: "/downloads/xinyue-cloud-phone-setup.exe",
    mobileTitle: "手机 / 平板 App",
    mobileHint: "手机扫码下载",
    mobileNote: "使用手机相机扫码，下载后按系统提示安装",
    windowsTitle: "Windows 客户端",
    windowsHint: "下载 Windows 安装包",
    windowsNote: "适用于 Windows 10 / 11",
  };

  function configValue(anchor, dataName, configName, fallback) {
    var attr = anchor.getAttribute("data-" + dataName);
    var globalConfig = window.CLIENT_DOWNLOAD_CONFIG || {};
    var value = attr || globalConfig[configName] || fallback;
    return typeof value === "string" ? value.trim() : value;
  }

  function mobileUrl(anchor) {
    var value = configValue(anchor, "mobile-download-url", "mobileUrl", "");
    if (!value) value = "/downloads/xinyue-cloud-phone-android.apk";
    try {
      return new URL(value, window.location.origin).href;
    } catch (_error) {
      return value;
    }
  }

  var qrLoader = null;

  function loadQrcode() {
    if (typeof window.qrcode === "function") return Promise.resolve();
    if (qrLoader) return qrLoader;
    qrLoader = new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = "/assets/qrcode-generator.js?v=1.4.4";
      script.async = true;
      script.onload = function () {
        if (typeof window.qrcode === "function") resolve();
        else {
          qrLoader = null;
          reject(new Error("QR generator unavailable"));
        }
      };
      script.onerror = function () {
        qrLoader = null;
        reject(new Error("QR generator load failed"));
      };
      document.head.appendChild(script);
    });
    return qrLoader;
  }

  function renderGeneratedQr(frame, url) {
    if (!frame || frame.getAttribute("data-qr-starting") === "true") return;
    frame.setAttribute("data-qr-starting", "true");
    while (frame.firstChild) frame.removeChild(frame.firstChild);

    var holder = document.createElement("span");
    holder.className = "client-download-qr-code";
    holder.textContent = "二维码";
    frame.appendChild(holder);

    loadQrcode().then(function () {
      var qr = window.qrcode(0, "H");
      qr.addData(url);
      qr.make();
      holder.innerHTML = qr.createSvgTag({
        cellSize: 4,
        margin: 0,
        scalable: true,
        alt: "手机客户端下载二维码",
      });

      var badge = document.createElement("span");
      badge.className = "client-download-qr-logo";
      badge.setAttribute("aria-hidden", "true");
      badge.textContent = "薪";
      frame.appendChild(badge);
      frame.removeAttribute("data-qr-starting");
    }).catch(function () {
      holder.textContent = "二维码暂不可用";
      holder.classList.add("client-download-qr-fallback");
      frame.removeAttribute("data-qr-starting");
    });
  }

  function createQrFrame(settings) {
    var frame = document.createElement("span");
    frame.className = "client-download-qr-frame";
    if (!settings.mobileQrImage) {
      frame.setAttribute("data-qr-url", settings.mobileUrl);
      return frame;
    }

    var image = document.createElement("img");
    image.className = "client-download-qr-image";
    image.src = settings.mobileQrImage;
    image.alt = "手机客户端下载二维码";
    image.addEventListener("error", function () {
      renderGeneratedQr(frame, settings.mobileUrl);
    }, { once: true });
    frame.appendChild(image);
    return frame;
  }

  function createMobilePanel(settings) {
    var link = document.createElement("a");
    link.className = "client-download-mobile";
    link.href = settings.mobileUrl;
    link.title = settings.mobileHint;

    var title = document.createElement("strong");
    title.className = "client-download-panel-title";
    title.textContent = settings.mobileTitle;
    var hint = document.createElement("span");
    hint.className = "client-download-panel-hint";
    hint.textContent = settings.mobileHint;
    var note = document.createElement("small");
    note.className = "client-download-mobile-note";
    note.textContent = settings.mobileNote;

    link.append(createQrFrame(settings), title, hint, note);
    return link;
  }

  function createDesktopPanel(settings) {
    var link = document.createElement("a");
    link.className = "client-download-desktop";
    link.href = settings.windowsUrl;
    link.download = "";
    link.title = settings.windowsHint;

    var icon = document.createElement("span");
    icon.className = "client-download-windows-frame";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = WINDOWS_LOGO;
    var title = document.createElement("strong");
    title.className = "client-download-panel-title";
    title.textContent = settings.windowsTitle;
    var hint = document.createElement("span");
    hint.className = "client-download-panel-hint";
    hint.textContent = settings.windowsHint;
    var action = document.createElement("span");
    action.className = "client-download-windows-action";
    action.textContent = "立即下载";
    var note = document.createElement("small");
    note.className = "client-download-desktop-note";
    note.textContent = settings.windowsNote;

    link.append(icon, title, hint, action, note);
    return link;
  }

  function createMenu(anchor) {
    var menu = document.createElement("div");
    menu.id = MENU_ID;
    menu.className = "client-download-menu";
    menu.setAttribute("role", "dialog");
    menu.setAttribute("aria-label", "下载客户端");

    var head = document.createElement("div");
    head.className = "client-download-menu-head";
    var title = document.createElement("strong");
    title.textContent = "客户端下载";
    var note = document.createElement("span");
    note.textContent = "官方安装包";
    head.append(title, note);

    var settings = {
      mobileUrl: mobileUrl(anchor),
      mobileQrImage: configValue(anchor, "mobile-qr-image", "mobileQrImage", DEFAULTS.mobileQrImage),
      windowsUrl: configValue(anchor, "windows-download-url", "windowsUrl", DEFAULTS.windowsUrl),
      mobileTitle: configValue(anchor, "mobile-title", "mobileTitle", DEFAULTS.mobileTitle),
      mobileHint: configValue(anchor, "mobile-hint", "mobileHint", DEFAULTS.mobileHint),
      mobileNote: configValue(anchor, "mobile-note", "mobileNote", DEFAULTS.mobileNote),
      windowsTitle: configValue(anchor, "windows-title", "windowsTitle", DEFAULTS.windowsTitle),
      windowsHint: configValue(anchor, "windows-hint", "windowsHint", DEFAULTS.windowsHint),
      windowsNote: configValue(anchor, "windows-note", "windowsNote", DEFAULTS.windowsNote),
    };
    var layout = document.createElement("div");
    layout.className = "client-download-layout";
    layout.append(createMobilePanel(settings), createDesktopPanel(settings));

    menu.append(head, layout);
    return menu;
  }

  function enhance(anchor) {
    var wrapper = document.createElement("div");
    wrapper.className = "client-download-wrap";

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "client-download-btn";
    trigger.title = "选择客户端下载平台";
    trigger.setAttribute("aria-label", "选择客户端下载平台");
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-controls", MENU_ID);
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("data-client-menu-ready", "true");

    while (anchor.firstChild) trigger.appendChild(anchor.firstChild);
    var menu = createMenu(anchor);
    anchor.replaceWith(wrapper);
    wrapper.append(trigger, menu);

    var pinned = false;
    var suppressHover = false;

    function render(open) {
      if (open) {
        var frames = wrapper.querySelectorAll(".client-download-qr-frame[data-qr-url]");
        Array.prototype.forEach.call(frames, function (frame) {
          var url = frame.getAttribute("data-qr-url");
          frame.removeAttribute("data-qr-url");
          renderGeneratedQr(frame, url);
        });
      }
      wrapper.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", String(open));
    }

    function close(blur) {
      pinned = false;
      render(false);
      if (blur && wrapper.contains(document.activeElement)) {
        document.activeElement.blur();
      }
    }

    wrapper.addEventListener("pointerenter", function () {
      if (!suppressHover) render(true);
    });
    wrapper.addEventListener("pointerleave", function () {
      suppressHover = false;
      if (!pinned && !wrapper.contains(document.activeElement)) render(false);
    });
    wrapper.addEventListener("focusin", function () {
      if (!suppressHover) render(true);
    });
    wrapper.addEventListener("focusout", function (event) {
      if (!wrapper.contains(event.relatedTarget) && !wrapper.matches(":hover")) {
        close(false);
      }
    });
    trigger.addEventListener("click", function () {
      if (pinned) {
        pinned = false;
        suppressHover = true;
        render(false);
      } else {
        pinned = true;
        render(true);
      }
    });
    menu.addEventListener("click", function () {
      setTimeout(function () {
        close(false);
      }, 0);
    });
    document.addEventListener("pointerdown", function (event) {
      if (!wrapper.contains(event.target)) close(false);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && wrapper.classList.contains("open")) {
        close(true);
        suppressHover = true;
      }
    });
  }

  function scan() {
    var anchor = document.querySelector(
      "a.client-download-btn:not([data-client-menu-ready])",
    );
    if (anchor) enhance(anchor);
  }

  var observer = new MutationObserver(function () {
    queueMicrotask(scan);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan, { once: true });
  } else {
    scan();
  }
})();
