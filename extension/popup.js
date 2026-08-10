const status = document.getElementById("status");
const version = document.getElementById("version");

// Read the extension version directly from manifest.json
const manifest = browser.runtime.getManifest();
version.textContent = `v${manifest.version}`;

function setStatus(text) {
    status.textContent = text;
}

document.getElementById("all").addEventListener("click", async () => {
    setStatus("Scanning tabs...");


    const response = await browser.runtime.sendMessage({
        action: "downloadAll"
    });

    if (!response) return setStatus("No response from background");
    if (response.error) return setStatus("Error: " + response.error);

    setStatus(`Sent ${response.count ?? 0} videos`);


});

document.getElementById("current").addEventListener("click", async () => {
    const response = await browser.runtime.sendMessage({
        action: "downloadCurrent"
    });


    if (!response) return setStatus("No response from background");
    if (response.error) return setStatus("Error: " + response.error);

    setStatus(response.message ?? JSON.stringify(response));


});

document.getElementById("specific").addEventListener("click", async () => {
    const response = await browser.runtime.sendMessage({
        action: "downloadSpecific"
    });


    if (!response) return setStatus("No response from background");
    if (response.error) return setStatus("Error: " + response.error);

    setStatus(response.message ?? `Sent ${response.count ?? 0}`);


});

document.getElementById("settings").addEventListener("click", async () => {
    setStatus("Opening settings...");


    try {
        const res = await fetch(
            "http://127.0.0.1:8765/settings/open",
            { method: "POST" }
        );

        if (!res.ok) {
            return setStatus("Failed to open settings");
        }

        const j = await res.json();
        setStatus(j.status ?? "Settings opened");

    } catch (e) {
        setStatus("Error opening settings: " + e.message);
    }


});
