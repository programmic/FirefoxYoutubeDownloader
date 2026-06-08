const SERVER_URL = "http://127.0.0.1:8765/enqueue";

const SERVER = "http://127.0.0.1:8765";

async function send(endpoint, data) {
    const res = await fetch(`${SERVER}/${endpoint}`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(data)
    });

    return res.ok;
}

async function getYoutubeTabs() {
    const tabs = await browser.tabs.query({
        url: "*://*.youtube.com/*"
    });

    return tabs;
}

async function downloadCurrent() {
    const tabs = await getYoutubeTabs();

    const tab = tabs[0];

    if (!tab?.url?.includes("youtube.com/watch")) {
        return {
            message: "Current tab is not a YouTube video"
        };
    }

    await send("enqueue", {
        url: tab.url
    });

    await send("command", {
        cmd: "!RUNALL"
    });

    return {
        message: "Current video queued"
    };
}

async function downloadAll() {
    const tabs = await getYoutubeTabs();

    let count = 0;

    for (const tab of tabs) {
        if (!tab.url?.includes("youtube.com/watch"))
            continue;

        if (await send("enqueue", { url: tab.url }))
            count++;
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
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ url: tab.url })
        });

        return res.ok;
    } catch (err) {
        console.error("Failed to enqueue tab:", tab.url, err);
        return false;
    }
}

async function enqueueAllTabs() {
    console.log("BUTTON CLICKED");

    await fetch("http://127.0.0.1:8765/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "BUTTON CLICKED" })
    });

    const tabs = await browser.tabs.query({});
    console.log("TABS FOUND:", tabs.length);

    let count = 0;

    for (const tab of tabs) {
        if (!tab.url) continue;
        if (!tab.url.includes("youtube.com/watch")) continue;

        const res = await fetch("http://127.0.0.1:8765/enqueue", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ url: tab.url })
        });

        if (res.ok) count++;
    }


    await fetch("http://127.0.0.1:8765/log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: `All found tabs (${count}) enqueued, starting to download...` })
    });

    await fetch("http://127.0.0.1:8765/command", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            cmd: "!RUNALL"
        })
    });
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
    await fetch(`${SERVER}/command`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cmd: `!SELECT ${signal}` })
    });

    return { count: urls.length, signal };
}

browser.browserAction.onClicked.addListener(enqueueAllTabs);