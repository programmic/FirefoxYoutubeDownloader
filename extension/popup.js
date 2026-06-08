const status = document.getElementById("status");

function setStatus(text) {
    status.textContent = text;
}

document.getElementById("all").addEventListener("click", async () => {
    setStatus("Scanning tabs...");

    const response = await browser.runtime.sendMessage({
        action: "downloadAll"
    });

    setStatus(`Sent ${response.count} videos`);
});

document.getElementById("current").addEventListener("click", async () => {
    const response = await browser.runtime.sendMessage({
        action: "downloadCurrent"
    });

    setStatus(response.message);
});

document.getElementById("specific").addEventListener("click", async () => {
    const response = await browser.runtime.sendMessage({
        action: "downloadSpecific"
    });

    setStatus(response.message);
});