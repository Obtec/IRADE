"""Real MCP server: each instance is scoped to one authenticated IRADE C++ connection.

It has no database credentials, public socket, filesystem/shell tools or ability to
choose another workspace/user. The C++ bridge rechecks ACLs and locks for each call.
"""

import json
from mcp.server.lowlevel import Server
from mcp.types import TextContent, Tool

TOOLS = [
    (
        "list_objects",
        "Search readable workspace cell summaries ordered cleanly by frameOrder; paginate instead of dumping everything.",
        {
            "query": {"type": "string"},
            "kind": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        [],
    ),
    (
        "get_objects",
        "Read up to 6 workspace cells by ID, including source (text/code) and revision. Read before editing.",
        {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 6,
            }
        },
        ["ids"],
    ),
    (
        "create_objects",
        "Create up to 10 workspace cells appended at the bottom unless frameOrder is specified. IRADE cell formats: Python (kind='code', language='python', renderMode='irade-python', code=...), Lean 4 (kind='code', language='lean4', renderMode='irade-lean', code=...), Rocq (kind='code', language='rocq', renderMode='irade-rocq', code=...), Markdown (kind='text', format='markdown', renderMode='irade-markdown', text=...), HTML (kind='text', format='html', renderMode='irade-html', text=...), Agent log (kind='text', format='markdown', renderMode='irade-agent', text=...).",
        {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {"type": "object"},
            }
        },
        ["items"],
    ),
    (
        "patch_objects",
        "Modify up to 10 existing workspace cells using the revision returned by get_objects. Include full updated source in text or code; preserve existing content when appending. Never guess a revision.",
        {
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "revision": {"type": "integer"},
                        "patch": {"type": "object"},
                    },
                    "required": ["id", "revision", "patch"],
                    "additionalProperties": False,
                },
            }
        },
        ["changes"],
    ),
    (
        "delete_objects",
        "Delete explicitly requested cells only; available only when the user enables deletion. Supply read revisions.",
        {
            "objects": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "revision": {"type": "integer"},
                    },
                    "required": ["id", "revision"],
                    "additionalProperties": False,
                },
            }
        },
        ["objects"],
    ),
]


def make_server(bridge, allow_delete=False):
    server = Server("irade-workspace-mcp")
    allowed = {
        name: (description, properties, required)
        for name, description, properties, required in TOOLS
        if name != "delete_objects" or allow_delete
    }

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=name,
                description=description,
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
            for name, (description, properties, required) in allowed.items()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name not in allowed:
            raise ValueError("Tool unavailable")
        result = await bridge(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server
