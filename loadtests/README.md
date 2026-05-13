# Load Testing

Use Locust to validate signup bursts and sustained API concurrency.

## Install

```bash
pip install -r loadtests/requirements.txt
```

## Example

```bash
locust -f loadtests/locustfile.py --host http://127.0.0.1:8000
```

## Suggested runs

- `100` concurrent signup users:
  `locust -f loadtests/locustfile.py --host http://127.0.0.1:8000 --users 100 --spawn-rate 20`
- `1000` concurrent API users:
  `LOADTEST_LOGIN_EMAIL=... LOADTEST_LOGIN_PASSWORD=... locust -f loadtests/locustfile.py --host http://127.0.0.1:8000 --users 1000 --spawn-rate 100`

## Notes

- Use a non-production Paystack/Firebase setup during tests.
- Point `DATABASE_URL` to PostgreSQL and `REDIS_URL` to Redis before running concurrency tests.
- Keep `PROCESS_ROLE=worker` on exactly one background-job process during multi-instance testing.
