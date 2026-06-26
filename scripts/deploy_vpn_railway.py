#!/usr/bin/env python3
"""Helper to manually redeploy Argos VPN API to Railway.

Railway GitHub auto-deploy is not connected, so this script refreshes the
service source to the latest main commit and triggers a fresh deploy.

Usage:
    export RAILWAY_API_TOKEN=...
    python scripts/deploy_vpn_railway.py
"""
from __future__ import annotations

import os
import sys
import time

import requests

API = "https://backboard.railway.app/graphql/v2"
PROJECT_ID = "88bd3ddc-a280-4e15-98a6-4b85e740c333"
SERVICE_ID = "9b7054b5-be6e-45fb-ad0b-5e838bd3b480"
ENV_ID = "1155be6c-1df2-413a-8542-b756415528a0"


def graphql(token: str, query: str) -> dict:
    resp = requests.post(
        API,
        json={"query": query},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def main() -> int:
    token = os.environ.get("RAILWAY_API_TOKEN") or os.environ.get("RAILWAY_TOKEN")
    if not token:
        print("Error: set RAILWAY_API_TOKEN or RAILWAY_TOKEN environment variable")
        return 1

    print("Refreshing source connection to origin/main...")
    graphql(
        token,
        f'mutation {{ serviceConnect(id: "{SERVICE_ID}", input: {{ repo: "poilopr57-a11y/Argos", branch: "main" }}) {{ id name }} }}',
    )

    print("Triggering deploy...")
    data = graphql(
        token,
        f'mutation {{ serviceInstanceDeployV2(environmentId: "{ENV_ID}", serviceId: "{SERVICE_ID}") }}',
    )
    deploy_id = data["serviceInstanceDeployV2"]
    print(f"Deployment {deploy_id} triggered")

    print("Waiting for build...")
    for _ in range(40):
        time.sleep(5)
        status = graphql(
            token,
            f'{{ deployment(id: "{deploy_id}") {{ status meta {{ commitHash commitMessage }} }} }}',
        )["deployment"]
        print(f"  {status['status']} — {status['meta']['commitMessage'][:60]}")
        if status["status"] in {"SUCCESS", "FAILED", "CRASHED"}:
            if status["status"] == "SUCCESS":
                print(f"Deploy succeeded: {status['meta']['commitHash'][:12]}")
                return 0
            return 1

    print("Timeout waiting for deployment status")
    return 1


if __name__ == "__main__":
    sys.exit(main())
