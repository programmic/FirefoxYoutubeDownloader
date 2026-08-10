const SERVER = (typeof browser !== 'undefined' && browser.runtime && browser.runtime.getURL)
    ? 'http://127.0.0.1:8765'
    : 'http://127.0.0.1:8765';

const SERVER_URL = `${SERVER}/enqueue`;

async function send(endpoint, data) {
    try {
        const res = await fetch(`${SERVER}/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        let json = null;
        try { json = await res.json(); } catch (e) { /* ignore */ }
        return { ok: res.ok, status: res.status, json };
    } catch (err) {
        console.error('Network error to server:', err);
        return { ok: false, error: String(err) };
    }
}

async function getYoutubeTabs() {
    const tabs = await browser.tabs.query({
        url: "*://*.youtube.com/*"
    });

    return tabs;
}

async function downloadCurrent() {
    // prefer the active tab in the current window (the tab where the popup was opened)
    const activeTabs = await browser.tabs.query({ active: true, currentWindow: true });
    const tab = activeTabs[0];

    if (!tab || !tab.url || !tab.url.includes("youtube.com/watch")) {
        return {
            message: "Current tab is not a YouTube video"
        };
    }

    const res = await send('enqueue', { url: tab.url });
    if (!res.ok) return { message: 'Failed to enqueue: ' + (res.error || res.status) };
    await send('command', { cmd: '!RUNALL' });
    return { message: 'Current video queued' };
}

async function downloadAll() {
    const tabs = await getYoutubeTabs();

    let count = 0;

    for (const tab of tabs) {
        if (!tab.url?.includes("youtube.com/watch"))
            continue;

        const res = await send('enqueue', { url: tab.url });
        if (res.ok) count++;
    }

    await send("command", {
        cmd: "!RUNALL"
    });

    return {
        count
    };
}



async function enqueueTab(tab) {
    try {
        const res = await fetch(SERVER_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: tab.url }),
        });
        return res.ok;
    } catch (err) {
        console.error("Failed to enqueue tab:", tab.url, err);
        return false;
    }
}

async function enqueueAllTabs() {
    console.log("BUTTON CLICKED");
    await fetch(`${SERVER}/log`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: 'BUTTON CLICKED' }) });

    const tabs = await browser.tabs.query({});
    console.log("TABS FOUND:", tabs.length);

    let count = 0;

    for (const tab of tabs) {
        if (!tab.url) continue;
        if (!tab.url.includes("youtube.com/watch")) continue;

        const res = await fetch(`${SERVER}/enqueue`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: tab.url }) });
        if (res.ok) count++;
    }


    await fetch(`${SERVER}/log`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: `All found tabs (${count}) enqueued, starting to download...` }) });
    await fetch(`${SERVER}/command`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: '!RUNALL' }) });
}

async function downloadSpecific() {
    // gather all youtube video urls
    const tabs = await browser.tabs.query({});
    const urls = [];

    for (const tab of tabs) {
        if (!tab.url) continue;
        if (!tab.url.includes("youtube.com/watch")) continue;
        urls.push(tab.url);
    }

    if (urls.length === 0) {
        return { message: "No YouTube video tabs found" };
    }

    // create a unique signal identifier for server-side selection handling
    const signal = `SELECT_${Date.now()}`;

    // send all urls as a single payload along with the signal
    await fetch(`${SERVER}/enqueue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls, signal })
    });

    // send a distinct command so the server can know to present selection for this signal
    await fetch(`${SERVER}/command`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cmd: `!SELECT ${signal}` }) });

    return { count: urls.length, signal };
}

// handle messages from popup
browser.runtime.onMessage.addListener((message, sender) => {
    if (!message || !message.action) return Promise.resolve({ error: 'no_action' });
    switch (message.action) {
        case 'downloadAll':
            return downloadAll();
        case 'downloadCurrent':
            return downloadCurrent();
        case 'downloadSpecific':
            return downloadSpecific();
        default:
            return Promise.resolve({ error: 'unknown_action' });
    }
});

// keep legacy click behavior: prefer `action.onClicked` (MV3), fall back to `browserAction` (MV2)
if (browser.action && browser.action.onClicked) {
    try {
        browser.action.onClicked.addListener(enqueueAllTabs);
    } catch (e) {
        console.warn('Failed to register action.onClicked:', e);
    }
} else if (browser.browserAction && browser.browserAction.onClicked) {
    try {
        browser.browserAction.onClicked.addListener(enqueueAllTabs);
    } catch (e) {
        console.warn('Failed to register browserAction.onClicked:', e);
    }
}