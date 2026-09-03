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
def eval(
    suite: str = typer.Argument(..., help="path to an eval suite YAML"),
    json_out: str = typer.Option("", "--json", help="also write the report as JSON here"),
    threshold: float = typer.Option(-1.0, help="override the suite's pass threshold (0-1)"),
) -> None:
    """Run an agent eval suite and print a report (exit 1 if below threshold)."""
    import asyncio

    from agentforge.bootstrap import build_agent_registry, build_llm
    from agentforge.evals import EvalRunner, load_suite, render_text, write_json
    from agentforge.observability import configure_observability

    async def _main() -> None:
        configure_observability()
        spec = load_suite(suite)
        if threshold >= 0:
            spec = spec.model_copy(update={"threshold": threshold})
        report = await EvalRunner(build_agent_registry(), build_llm()).run_suite(spec)
        typer.echo(render_text(report))
        if json_out:
            write_json(report, json_out)
        raise typer.Exit(0 if report.passed else 1)

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
