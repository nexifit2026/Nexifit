import os
import multiprocessing

# Server socket
bind = f"0.0.0.0:{os.environ.get('PORT', 8080)}"
backlog = 2048

# Worker processes
workers = 1  # CRITICAL: Only 1 worker to avoid duplicate schedulers
worker_class = 'sync'
worker_connections = 1000
timeout = 120
keepalive = 5

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Process naming
proc_name = 'nexifit'

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL (if needed)
keyfile = None
certfile = None

def on_starting(server):
    """Called just before the master process is initialized."""
    print("="*70)
    print("🚀 NexiFit Bot Starting...")
    print("="*70)

def when_ready(server):
    """Called just after the server is started."""
    print("="*70)
    print("✅ NexiFit Bot Ready!")
    print(f"📡 Listening on: {bind}")
    print("="*70)

def on_exit(server):
    """Called just before exiting."""
    print("="*70)
    print("👋 NexiFit Bot Shutting Down...")
    print("="*70)
