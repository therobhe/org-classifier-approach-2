import asyncio
import typer
from pathlib import Path
import logging
from typing import Optional

from .pipeline import ClassificationPipeline
from .config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

app = typer.Typer(
    name="org-classifier",
    help="German legal-form classification pipeline",
    add_completion=False
)


def _run_classify_sync(
    input_csv: Path,
    output_csv: Path,
    org_column: str,
    offline: bool,
    max_workers: Optional[int],
    cache_dir: Optional[str],
    clear_cache: bool,
) -> None:
    async def run():
        pipeline_cache_dir = cache_dir or settings.cache_dir
        pipeline_max_workers = max_workers or settings.max_concurrent_requests

        async with ClassificationPipeline(
            cache_dir=pipeline_cache_dir,
            max_concurrent_requests=pipeline_max_workers,
            offline=offline,
        ) as pipeline:
            if clear_cache:
                typer.echo("Clearing cache...")
                pipeline.cache.clear()

            typer.echo(f"Processing: {input_csv}")
            if offline:
                typer.echo("Running in OFFLINE mode (web search disabled)")

            await pipeline.process_csv(
                input_path=input_csv,
                output_path=output_csv,
                org_column=org_column,
            )

            typer.echo(f"✓ Results written to: {output_csv}")

    asyncio.run(run())


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    input_csv: Optional[Path] = typer.Argument(
        None,
        help="Path to input CSV file with organisation names",
    ),
    output_csv: Optional[Path] = typer.Argument(
        None,
        help="Path to output CSV file with classifications",
    ),
    org_column: str = typer.Option(
        "organisation_name",
        "--column",
        "-c",
        help="Name of column containing organisation names (auto-detected if missing)",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Skip web search stage (regex + heuristics only)",
    ),
    max_workers: Optional[int] = typer.Option(
        None,
        "--max-workers",
        "-w",
        help="Maximum concurrent web requests (default: 5)",
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Directory for caching results (default: .cache)",
    ),
    clear_cache: bool = typer.Option(
        False,
        "--clear-cache",
        help="Clear cache before processing",
    ),
):
    """Default command.

    If no subcommand is provided, runs classification:
        org-classifier input.csv output.csv --offline

    Subcommands are still available:
        org-classifier clear-cache
        org-classifier version
    """
    if ctx.invoked_subcommand is not None:
        return

    if input_csv is None or output_csv is None:
        raise typer.BadParameter(
            "Missing arguments. Usage: org-classifier <input_csv> <output_csv> [--offline]"
        )

    if not input_csv.exists():
        raise typer.BadParameter(f"Input file does not exist: {input_csv}")

    _run_classify_sync(
        input_csv=input_csv,
        output_csv=output_csv,
        org_column=org_column,
        offline=offline,
        max_workers=max_workers,
        cache_dir=cache_dir,
        clear_cache=clear_cache,
    )


@app.command()
def classify(
    input_csv: Path = typer.Argument(
        ...,
        help="Path to input CSV file with organisation names",
        exists=True,
        dir_okay=False
    ),
    output_csv: Path = typer.Argument(
        ...,
        help="Path to output CSV file with classifications"
    ),
    org_column: str = typer.Option(
        "organisation_name",
        "--column", "-c",
        help="Name of column containing organisation names"
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Skip web search stage (regex + heuristics only)"
    ),
    max_workers: Optional[int] = typer.Option(
        None,
        "--max-workers", "-w",
        help="Maximum concurrent web requests (default: 5)"
    ),
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Directory for caching results (default: .cache)"
    ),
    clear_cache: bool = typer.Option(
        False,
        "--clear-cache",
        help="Clear cache before processing"
    )
):
    """
    Classify German organisations by their legal form (Rechtsform).
    
    Reads a CSV with organisation names and produces an enriched CSV
    with legal_form, confidence, and source columns.
    
    Examples:
    
        # Basic usage
        org-classifier input.csv output.csv
        
        # Offline mode (no web search)
        org-classifier input.csv output.csv --offline
        
        # Custom settings
        org-classifier input.csv output.csv --max-workers 10 --column name
    """
    _run_classify_sync(
        input_csv=input_csv,
        output_csv=output_csv,
        org_column=org_column,
        offline=offline,
        max_workers=max_workers,
        cache_dir=cache_dir,
        clear_cache=clear_cache,
    )


@app.command()
def clear_cache(
    cache_dir: Optional[str] = typer.Option(
        None,
        "--cache-dir",
        help="Directory containing cache to clear (default: .cache)"
    )
):
    """Clear the classification cache."""
    from .cache import ClassificationCache
    
    target_dir = cache_dir or settings.cache_dir
    cache = ClassificationCache(target_dir)
    cache.clear()
    cache.close()
    typer.echo(f"✓ Cache cleared: {target_dir}")


@app.command()
def version():
    """Show version information."""
    from . import __version__
    typer.echo(f"org-classifier v{__version__}")


if __name__ == "__main__":
    app()
