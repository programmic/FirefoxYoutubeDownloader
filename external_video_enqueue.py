import pathlib
import sqlite3
import time
import requests
from tqdm import tqdm

# ==================== CONFIGURATION ====================
# Replace with 'localhost' if running on the same machine, 
# or the local IP (e.g., '192.168.1.50') if running on the same Wi-Fi network.
SERVER_IP = "localhost" 
PORT = 8765
BASE_URL = f"http://{SERVER_IP}:{PORT}"
# =======================================================

def send_log(message: str):
    """Sends a debug log message to the server terminal."""
    try:
        requests.post(f"{BASE_URL}/log", json={"text": f"[Util-Script] {message}"}, timeout=5)
    except requests.exceptions.RequestException:
        pass  # Quietly pass if logs aren't critically required

def enqueue_video(url: str) -> bool:
    """Enqueues a single YouTube video URL."""
    print(f"Sending video to queue: {url}")
    try:
        response = requests.post(
            f"{BASE_URL}/enqueue", 
            json={"url": url}, 
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if response.status_code == 200:
            print(f"\033[92m[ OK ]\033[0m Successfully queued!")
            send_log(f"Successfully queued video: {url}")
            return True
        else:
            print(f"\033[91m[ERR ]\033[0m Server rejected request (Status: {response.status_code})")
            return False
    except requests.exceptions.RequestException as e:
        print(f"\033[91m[ERR ]\033[0m Network Error: Could not connect to server at {BASE_URL}. Is it running?")
        print(f"Details: {e}")
        return False

def enqueue_multiple_with_selection(urls: list[str]):
    """
    Sends an array of URLs and immediately issues a selection command,
    matching the exact structural logic of the extension's 'downloadSpecific' action.
    """
    if not urls:
        print(f"\033[91m[ERR ]\033[0m No URLs provided.")
        return

    signal = f"SELECT_{int(time.time() * 1000)}"
    print(f"Sending {len(urls)} videos with tracking signal: {signal}")

    try:
        enqueue_res = requests.post(
            f"{BASE_URL}/enqueue",
            json={"urls": urls, "signal": signal},
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        
        command_res = requests.post(
            f"{BASE_URL}/command",
            json={"cmd": f"!SELECT {signal}"},
            headers={"Content-Type": "application/json"},
            timeout=5
        )

        if enqueue_res.status_code == 200 and command_res.status_code == 200:
            print(f"\033[92m[ OK ]\033[0m Batched transmission successful! Signal tracking ID: {signal}")
            send_log(f"Batched selection sent for {len(urls)} videos via API.")
        else:
            print(f"\033[93m[WARN]\033[0m Server processed batch with anomalies.")
            print(f"Enqueue Status: {enqueue_res.status_code} | Command Status: {command_res.status_code}")

    except requests.exceptions.RequestException as e:
        print(f"\033[91m[ERR ]\033[0m Network Error during batch execution: {e}")

def load_urls_from_databank(databank_path: pathlib.Path = pathlib.Path(__file__).parent / "downloads.db") -> list[str]:
    """
    Loads a list of URLs from a local SQLite databank file.
    Queries the 'videos' table to fetch the target URLs.
    """
    if not databank_path.exists():
        print(f"\033[91m[ERR ]\033[0m Databank file not found at {databank_path}.")
        return []

    urls: list[str] = []
    
    try:
        conn = sqlite3.connect(databank_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM videos")
        total_rows = cursor.fetchone()[0]
        
        if total_rows == 0:
            print(f"\033[93m[WARN]\033[0m Databank is empty. No URLs to enqueue.")
            conn.close()
            return []

        cursor.execute("SELECT URL FROM videos")
        
        for row in tqdm(cursor.fetchall(), total=total_rows, desc="Loading URLs"):
            if row[0]:
                urls.append(row[0].strip())
                
        conn.close()
        
    except sqlite3.Error as e:
        print(f"\033[91m[ERR ]\033[0m Database error: {e}")
        return []
    except Exception as e:
        print(f"\033[91m[ERR ]\033[0m Unexpected error reading database: {e}")
        return []

    if len(urls) > 100:
        print(f"\033[93m[WARN]\033[0m Databank contains {len(urls)} URLs. Consider batching or filtering to avoid overwhelming the server.")
    
    if urls and "https://www.youtube.com/watch?v=" not in urls[0] and "youtu.be/" not in urls[0]:
        print(f"\033[93m[WARN]\033[0m The first URL in the databank does not appear to be a valid YouTube link: {urls[0]}")
    
    print(f"Loaded {len(urls)} URLs from databank.")
    return urls

# ==================== USAGE EXAMPLES ====================
if __name__ == "__main__":
    databank_path = pathlib.Path(__file__).parent / "downloads.db"
    video_batch = load_urls_from_databank()
    enqueue_multiple_with_selection(video_batch)
