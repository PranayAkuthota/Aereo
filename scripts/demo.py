"""Exercise the three required API operations with the checked-in synthetic examples."""

import argparse
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def request_json(request: str | Request) -> dict:
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    examples = Path(__file__).resolve().parents[1] / "examples"
    try:
        for name in ("polygon.kml", "lines.kml", "points.kml", "sample_shapefile.zip"):
            boundary = uuid4().hex
            body = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                    f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode()
                + (examples / name).read_bytes()
                + f"\r\n--{boundary}--\r\n".encode()
            )
            request = Request(
                base + "/api/files/",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            uploaded = request_json(request)
            location = base + f"/api/files/{uploaded['id']}/"
            metadata = request_json(location)
            measurements = request_json(location + "measurements/")
            if metadata != uploaded or measurements["total"] != uploaded["feature_count"]:
                raise SystemExit("Demo metadata/result consistency check failed.")
            print(json.dumps({"sample": name, "metadata": metadata, "measurements": measurements}))
    except HTTPError as exc:
        raise SystemExit(
            f"API returned HTTP {exc.code}; check server logs and configured limits."
        ) from exc
    except URLError as exc:
        raise SystemExit("API unavailable; start Uvicorn or Docker first.") from exc


if __name__ == "__main__":
    main()
