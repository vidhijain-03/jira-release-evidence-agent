import os, sys, json
from pathlib import Path
from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[2]
@asynccontextmanager
async def jira_session(project_key, dataset_label):
    env={k:v for k,v in os.environ.items() if k.startswith('JIRA_')}
    env.update(JIRA_PROJECT_KEY=project_key,JIRA_DATASET_LABEL=dataset_label,
               AGENT_MAX_BATCH_ISSUES=os.getenv('AGENT_MAX_BATCH_ISSUES','10'))
    params=StdioServerParameters(command=sys.executable,args=['-m','src.mcp_server.jira_server'],cwd=str(ROOT),env=env)
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            yield session

def decode(result):
    if result.isError: raise RuntimeError('MCP tool returned an error')
    if result.structuredContent is not None: return result.structuredContent
    blocks=[c.text for c in result.content if c.type=='text']
    if len(blocks)!=1: raise ValueError('Unexpected MCP response')
    return json.loads(blocks[0])
