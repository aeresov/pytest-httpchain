import click

from pytest_http_mcp.server import mcp


@click.command()
def serve():
    mcp.run()
