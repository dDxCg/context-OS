from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router.v1.vcs import router as vcs_router

local_ports = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=local_ports,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(vcs_router)


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
