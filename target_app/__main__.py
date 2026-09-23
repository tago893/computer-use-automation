"""Run the target app: ``python -m target_app``.

Binds to 127.0.0.1 by default. The app has no authentication worth the name and
exists to be automated, so it must never be exposed beyond the loopback
interface.
"""

from __future__ import annotations

from config import app_settings

from .app import create_app


def main() -> None:
    settings = app_settings()
    app = create_app()
    print(f"target app -> {settings.base_url}/t/{settings.tenant}/login")
    print(f"tenants    -> {settings.base_url}/t/meridian/login , /t/northgate/login")
    # debug=False: the Werkzeug debugger is a remote code execution endpoint and
    # this process is deliberately driven by an automated agent.
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
