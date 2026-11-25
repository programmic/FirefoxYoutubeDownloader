
console.log(
    "===================================="
);

console.log(
    "[YT SMART DOWNLOADER] SCRIPT LOADED"
);


console.log(
    "===================================="
);


/* DEBUG START


document.addEventListener("click", (event) => {

    const target = event.target;

    console.log("========== YOUTUBE CLICK ==========");
    console.log("Target:", target);
    console.log("Tag:", target.tagName);

    let element = target;

    for (let i = 0; element && i < 15; i++) {

        console.log(`ANCESTOR ${i}:`, {
            tag: element.tagName,
            id: element.id,
            className: element.className,
            attributes: [...element.attributes].reduce(
                (obj, attr) => {
                    obj[attr.name] = attr.value;
                    return obj;
                },
                {}
            )
        });

        element = element.parentElement;
    }

    console.log("===================================");

}, true);

// DEBUG END */


(() => {
    "use strict";

    const ITEM_ID = "yt-smart-downloader-item";
    const LABEL = "Smart Download";



    let activeVideoUrl = null;

    document.addEventListener("click", (event) => {

        // Ignore our own download menu.
        if (event.target.closest("#yt-smart-downloader-item")) {
            return;
        }

        // Find YouTube's new menu button.
        const menuButton = event.target.closest(
            ".ytLockupMetadataViewModelMenuButton"
        );

        if (!menuButton) return;

        // Find the associated video lockup.
        const lockup = menuButton.closest(
            ".ytLockupViewModelHost"
        );

        if (!lockup) {
            console.warn(
                "[YT Smart Downloader] Lockup not found"
            );
            return;
        }

        // Extract the video ID from content-id-VIDEO_ID.
        const match = lockup.className.match(
            /(?:^|\s)content-id-([a-zA-Z0-9_-]{11})(?:\s|$)/
        );

        if (!match) {
            console.warn(
                "[YT Smart Downloader] Video ID not found",
                lockup.className
            );
            return;
        }

        const videoId = match[1];

        activeVideoUrl =
            `https://www.youtube.com/watch?v=${videoId}`;

        console.log(
            "[YT Smart Downloader] Captured video:",
            activeVideoUrl
        );

    }, true);



    // ==========================================
    // CREATE DOWNLOAD MENU ITEM
    // ==========================================

    function createMenuItem() {

        const item = document.createElement(
            "yt-list-item-view-model"
        );

        item.id = ITEM_ID;
        item.className = "ytListItemViewModelHost";

        item.setAttribute(
            "role",
            "presentation"
        );

        const button = document.createElement("button");

        button.type = "button";

        button.setAttribute(
            "role",
            "menuitem"
        );

        button.className = [
            "ytButtonOrAnchorHost",
            "ytButtonOrAnchorButton",
            "ytListItemViewModelButtonOrAnchor",
            "ytListItemViewModelTextWrapper"
        ].join(" ");

        button.style.cssText = `
            width: 100%;
            padding: 12px;
            color: inherit;
            background: transparent;
            border: none;
            text-align: left;
            cursor: pointer;
        `;

        const title = document.createElement("span");

        title.className = "ytListItemViewModelTitle";
        title.textContent = LABEL;

        button.appendChild(title);
        item.appendChild(button);


        // ==========================================
        // DOWNLOAD CLICK HANDLER
        // ==========================================

        button.addEventListener("click", async () => {

            if (!activeVideoUrl) {
                title.textContent = "Could not identify video";
                return;
            }

            button.disabled = true;

            title.textContent = "Queuing download...";

            try {

                const result = await browser.runtime.sendMessage({
                    action: "downloadUrl",
                    url: activeVideoUrl
                });

                title.textContent =
                    result?.message || "Download request sent";

                console.log(
                    "[YT Smart Downloader]",
                    result
                );

            } catch (error) {

                console.error(
                    "[YT Smart Downloader]",
                    error
                );

                title.textContent = "Download failed";
            }

            setTimeout(() => {

                if (!item.isConnected) return;

                button.disabled = false;
                title.textContent = LABEL;

            }, 2500);

        });

        return item;
    }


    // ==========================================
    // PROCESS YOUTUBE MENUS
    // ==========================================

    function processMenus() {

        const menus = document.querySelectorAll(
            "yt-list-view-model"
        );

        for (const menu of menus) {

            const nativeDownload = menu.querySelector(
                ":scope > yt-download-list-item-view-model"
            );

            if (!nativeDownload) continue;

            if (menu.querySelector(`#${ITEM_ID}`)) continue;

            const item = createMenuItem();

            nativeDownload.after(item);

            console.log(
                "[YT Smart Downloader] Successfully injected!"
            );
        }
    }


    // ==========================================
    // MUTATION OBSERVER
    // ==========================================

    let scheduled = false;

    function scheduleProcessing() {

        if (scheduled) return;

        scheduled = true;

        requestAnimationFrame(() => {

            scheduled = false;

            processMenus();

        });
    }

    const observer = new MutationObserver(
        scheduleProcessing
    );

    observer.observe(document.documentElement, {

        childList: true,
        subtree: true,
        attributes: true,

        attributeFilter: [
            "style",
            "class",
            "hidden"
        ]

    });


    // ==========================================
    // INITIALIZE
    // ==========================================

    processMenus();

    console.log(
        "[YT Smart Downloader] Initialized"
    );

})();
