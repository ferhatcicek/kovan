"""Portu dinler, bağlantıyı kabul eder ama hiç yanıt vermez (donmuş uygulama benzetimi)."""
import os
import socket
import time

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(os.environ["PORT"])))
s.listen(50)
print("dinleniyor", flush=True)
conns = []
while True:
    c, _ = s.accept()
    conns.append(c)  # yanıt yok
    time.sleep(0.01)


if __name__ == "__main__":
    pass
