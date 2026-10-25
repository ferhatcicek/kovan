from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    print("istek geldi")
    return {"ok": True}
