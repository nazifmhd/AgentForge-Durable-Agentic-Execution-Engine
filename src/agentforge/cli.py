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
    """Run the FastAPI server."""
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
def apikey(
    tenant: str = typer.Option(..., help="tenant id this key belongs to"),
    name: str = typer.Option("cli-key", help="human label for the key"),
    scopes: str = typer.Option("admin", help="comma-separated scopes, or 'admin'"),
) -> None:
    """Mint an API key for a tenant and print it once."""
    import asyncio

    from agentforge.core.auth import PgApiKeyStore, mint_api_key
    from agentforge.db import dispose_engine, get_sessionmaker

    async def _main() -> None:
        plaintext, record = mint_api_key(
            tenant_id=tenant,
            name=name,
            scopes=[s.strip() for s in scopes.split(",") if s.strip()],
        )
        try:
            await PgApiKeyStore(get_sessionmaker()).create(record)
        finally:
            await dispose_engine()
        typer.echo(plaintext)

    asyncio.run(_main())


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
