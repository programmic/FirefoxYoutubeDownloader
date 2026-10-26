import asyncio

# Queue will be created on application startup so it's bound to the running event loop
queue = None

# In-memory mapping for pending "selection" payloads sent by the extension.
# Key: signal string (e.g. "SELECT_..."), Value: list of urls
pending_selections = {}

# Running event loop reference (set on app startup) so other threads can schedule
# safe calls into the asyncio loop (e.g. enqueueing items from a Tk GUI thread).
loop = None

# Heartbeat flag to pause heartbeat during certain tasks (e.g. fetching video titles in the selector) to reduce noise in logs.
heartbeat = True

# Number of downloads currently being processed by queue workers.
active_downloads = 0