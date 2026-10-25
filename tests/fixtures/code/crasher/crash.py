import os

print("yapılandırma okunuyor")


def load():
    return os.environ["DB_URL_YOK"]


if __name__ == "__main__":
    load()
