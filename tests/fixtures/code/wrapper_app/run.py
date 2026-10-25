"""Sarmalayıcı süreç modeli (Windows venv başlatıcısı, `uv run`, bazı reloader'lar): asıl işi bir alt süreç
yapar; alt süreç ölürse sarmalayıcı yaşamaya devam eder ve onu yeniden başlatmaz."""
import subprocess
import sys
import time

if __name__ == "__main__":
    child = subprocess.Popen([sys.executable, "worker.py"])
    print(f"işçi başlatıldı pid={child.pid}", flush=True)
    while True:
        time.sleep(1)
