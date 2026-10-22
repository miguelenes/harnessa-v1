"""Container readiness check for the API service."""

from urllib.error import URLError
from urllib.request import urlopen


def main() -> None:
    try:
        with urlopen("http://127.0.0.1:8000/health/ready", timeout=2) as response:
            if response.status != 200:
                raise SystemExit(1)
    except URLError as exc:
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
