import sys
import time

print("\x1b[32mbot başladı\x1b[0m", flush=True)
i = 0
while True:
    i += 1
    print(f"tick {i}")
    if i % 3 == 0:
        print(f"uyarı {i} token=abc123secret", file=sys.stderr)
    time.sleep(0.2)


if __name__ == "__main__":
    pass
