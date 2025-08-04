import click

from pytest_httpchain_mcp.server import mcp


@click.command()
def serve():
    mcp.run()
