from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.getenv("MOSS_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("MOSS_GATEWAY_PORT", "8765"))
    uvicorn.run(
        "moss_gateway.app:app",
        host=host,
        port=port,
        reload=False,
        access_log=True,
    )


if __name__ == "__main__":
    main()
