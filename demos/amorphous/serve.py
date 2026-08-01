#!/usr/bin/env python3
"""Entry point: python demos/amorphous/serve.py [--port 8877] [--curator-minutes 360]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    import server as amorphous_server
    amorphous_server.main()
