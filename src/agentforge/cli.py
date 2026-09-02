"""``agentforge`` command-line entrypoint."""

from __future__ import annotations

import typer

from agentforge import __version__
from agentforge.logging import get_logger

app = typer.Typer(add_completion=False, help="AgentForge durable execution engine.")
log = get_logger("cli")


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def api() -> None:
    """Run the FastAPI server (implemented in Phase 6)."""
    import uvicorn

    from agentforge.config import settings

    uvicorn.run(
        "agentforge.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


@app.command()
def worker() -> None:
    """Run an execution worker: claim leases, drive workflows, heartbeat, recover."""
    import asyncio

    from agentforge.bootstrap import build_worker
    from agentforge.db import dispose_engine

    async def _main() -> None:
        w = build_worker()
        w.install_signal_handlers()
        try:
            await w.run()
        finally:
            await dispose_engine()

    asyncio.run(_main())


if __name__ == "__main__":
    app()
