from __future__ import annotations

import os
import random
import uuid

from locust import HttpUser, between, task


API_PREFIX = os.getenv("API_PREFIX", "/api/v1")
SIGNUP_PASSWORD = os.getenv("LOADTEST_SIGNUP_PASSWORD", "Password123!")
LOGIN_EMAIL = os.getenv("LOADTEST_LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.getenv("LOADTEST_LOGIN_PASSWORD", "")


class QringApiUser(HttpUser):
    wait_time = between(0.2, 1.0)

    def on_start(self):
        self.access_token = None
        if LOGIN_EMAIL and LOGIN_PASSWORD:
            response = self.client.post(
                f"{API_PREFIX}/auth/login",
                json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
                name="auth_login",
            )
            if response.ok:
                data = response.json().get("data") or {}
                self.access_token = data.get("accessToken")

    @task(2)
    def health(self):
        self.client.get(f"{API_PREFIX}/health", name="health")

    @task(1)
    def signup(self):
        email = f"loadtest-{uuid.uuid4().hex[:12]}@example.com"
        payload = {
            "fullName": "Load Test User",
            "email": email,
            "password": SIGNUP_PASSWORD,
            "role": random.choice(["homeowner", "estate"]),
        }
        self.client.post(f"{API_PREFIX}/auth/signup", json=payload, name="auth_signup")

    @task(3)
    def homeowner_overview(self):
        if not self.access_token:
            return
        self.client.get(
            f"{API_PREFIX}/dashboard/overview",
            headers={"Authorization": f"Bearer {self.access_token}"},
            name="dashboard_overview",
        )

    @task(2)
    def notifications(self):
        if not self.access_token:
            return
        self.client.get(
            f"{API_PREFIX}/notifications",
            headers={"Authorization": f"Bearer {self.access_token}"},
            name="notifications",
        )
