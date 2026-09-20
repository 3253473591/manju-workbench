"""列出某集的提交与 attempt，用于观察真实链路里每一步的耗时与结果。"""

from __future__ import annotations

import os
import sys

import httpx

PORT = os.environ.get("MANJU_BACKEND_PORT", "8010")
BASE = f"http://127.0.0.1:{PORT}/api/v1"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：inspect_submissions.py <episodeId>")
        return 2
    episode_id = sys.argv[1]
    client = httpx.Client(base_url=BASE, timeout=60)
    token = client.post(
        "/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["sessionToken"]
    client.headers["Authorization"] = f"Bearer {token}"

    items = client.get(
        f"/episodes/{episode_id}/submissions", params={"pageSize": 50}
    ).json()["items"]
    for submission in reversed(items):
        print(
            f"{submission['createdAt']}  {submission['operation']:22} "
            f"{submission['logicalStatus']:9} {submission.get('errorCode') or ''} "
            f"{submission.get('errorMessage') or ''}"
        )
        detail = client.get(f"/submissions/{submission['id']}").json()
        for attempt in detail["attempts"]:
            created = attempt["createdAt"][11:19]
            finished = (attempt["finishedAt"] or "")[11:19]
            print(
                f"    #{attempt['attemptNo']} {attempt['physicalStatus']:9} "
                f"{attempt['deliveryStatus']:8} {created}->{finished} "
                f"usage={attempt['usage']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
