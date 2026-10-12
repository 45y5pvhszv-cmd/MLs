"""Safe entry point for the MLS video automation scaffold.

No external APIs are called and nothing is uploaded by default.
"""
import os
from datetime import datetime, timezone


def main() -> None:
    publish_mode = os.getenv("PUBLISH_MODE", "disabled").strip().lower()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print("MLS Soccer Video Automation")
    print(f"UTC run time: {now}")
    print(f"Publish mode: {publish_mode}")

    if publish_mode != "disabled":
        print(
            "Publishing is intentionally not implemented in this scaffold. "
            "Configure and test the full pipeline before enabling uploads."
        )
    else:
        print(
            "Safe starter run complete. No data APIs were called and no videos "
            "were generated or uploaded."
        )


if __name__ == "__main__":
    main()
