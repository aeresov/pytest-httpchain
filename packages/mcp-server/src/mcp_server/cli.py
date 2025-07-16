import click

from mcp_server.server import mcp


@click.command()
def serve():
    mcp.run()
