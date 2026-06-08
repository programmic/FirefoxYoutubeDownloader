from pathlib import Path
from yt_dlp import YoutubeDL

VIDEO_FOLDER = r"C:\Users\Simon\Documents\Python\FirefoxYoutubeDownloader\failed_downloads"

ydl_opts = {
    "quiet": True,
    "extract_flat": True,
}

def add_to_link_file(url):
    with open("links.txt", "a") as f:
        f.write(url + "\n")

with YoutubeDL(ydl_opts) as ydl:
    for pattern in ["*.mp4", "*.webm"]:
        for file in Path(VIDEO_FOLDER).glob(pattern):
            query = file.stem

            result = ydl.extract_info(
                f"ytsearch1:{query}",
                download=False
            )

            if result["entries"]:
                video = result["entries"][0]
                url = f"https://www.youtube.com/watch?v={video['id']}"

                print(query)
                print(url)
                add_to_link_file(url)
                print()