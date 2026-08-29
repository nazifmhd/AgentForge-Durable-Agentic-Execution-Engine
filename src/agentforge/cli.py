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
    """Run an execution worker (implemented in Phase 2)."""
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
