# OPC UA MCP Server

An MCP server that connects to OPC UA-enabled industrial systems, allowing AI agents to monitor, analyze, and control operational data in real time.

This project is ideal for developers and engineers looking to bridge AI-driven workflows with industrial automation systems (Siemens S7 / TIA Portal in particular).

![GitHub License](https://img.shields.io/github/license/kukapay/opcua-mcp)
![Python Version](https://img.shields.io/badge/python-3.13+-blue)
![Status](https://img.shields.io/badge/status-active-brightgreen.svg)

## Features

- **Startup node awareness**: The address space is parsed from a UANodeSet XML export (`opc/testOPCTree.xml` or your own via `OPCUA_TREE_FILE`) when the server starts, so the agent immediately knows every available node — even before / without a live server connection.
- **Node discovery by friendly name**: Search the catalogue by browse name, node id, or Siemens S7 mapping path (e.g. `DB14 ...`).
- **Direct values, no opaque blobs**: Structure-typed nodes (e.g. `NMEA Data`) normally come back as an opaque `ExtensionObject` binary payload. The server decodes them on the fly using the struct field layout from the XML (`<Definition>` blocks) and returns readable JSON fields.
- **Namespace remapping**: Siemens servers often renumber namespaces (the XML export says `ns=2`, the live server exposes the same nodes as `ns=4`). On connect the server maps XML namespaces to live ones by URI (`get_namespace_array()`), so every node id is automatically translated — it falls back to a live name-based lookup if the URIs differ.
- **Read OPC UA Nodes**: Retrieve real-time values from industrial devices (accepts a node id, browse name, or path).
- **Write to OPC UA Nodes**: Control devices by writing values to specified nodes.
- **Browse nodes**: List children of any node — from the preloaded tree (offline) or the live server.
- **Read multiple OPC UA Nodes**: Retrieve multiple real-time values in a single request.
- **Write to multiple OPC UA Nodes**: Control devices by writing values to multiple nodes in a single request.
- **Seamless Integration**: Works with MCP clients like Claude Desktop for natural language interaction.

## How it works

```
UANodeSet XML export          live OPC UA server (e.g. S7-1500)
 (opc/*.xml)                       │
      │                            │
      ▼                            ▼
┌────────────────┐          ┌───────────────┐
│  preloaded     │          │  on connect:  │
│  node catalogue│◄────────►│ namespace map │
│  (opc_tree.py) │          │  (XML↔live)   │
└────────┬───────┘          └───────┬───────┘
         │                          │
         ▼                          ▼
  search / list / info      read / write / browse
  (works offline)           with auto id translation
                                  │
                                  ▼
                          ExtensionObject values
                          are decoded via the XML
                          struct definitions
```

1. **At startup** the exported `UANodeSet` XML is parsed into an in-memory index: every node (id, browse name, class, data type, access level, S7 mapping path), the parent/child hierarchy, and the structure definitions (`<Definition>` field layouts).
2. **On connect** the live server's namespace array is compared to the XML's namespace URIs, producing a translation table (e.g. `XML ns=2 → live ns=4`). A name-based browse index is built only if the URIs don't match.
3. **On read**, friendly references are resolved to concrete node ids, translated to the live namespace, and read. Structure-typed values are decoded field-by-field into JSON using the XML definition — you never see raw binary blobs.

## Tools

### Node catalogue (work offline from the preloaded tree)

| Tool | Description |
|---|---|
| `search_opcua_nodes` | Search nodes by name / node id / S7 mapping path. |
| `list_opcua_nodes` | List nodes, optionally filtered by class, parent, or substring. |
| `get_opcua_node_info` | Full metadata for one node (parent, data type, writability, S7 path, children). |
| `list_opcua_children` | List the children of a node (preloaded tree, works offline). |
| `get_opcua_tree` | Nested view of the hierarchy from any root node. |
| `reload_opcua_tree` | Reload the catalogue from an XML file at runtime. |

### Live tools (require a connection)

| Tool | Description |
|---|---|
| `read_opcua_node` | Read a node's value. Scalar nodes return their value directly; struct nodes (e.g. `NMEA Data`) are decoded into readable JSON. |
| `write_opcua_node` | Write a value to a node. |
| `browse_opcua_node_children` | Browse children (preloaded tree first, live server fallback). |
| `read_multiple_opcua_nodes` | Read several nodes in one request. |
| `write_multiple_opcua_nodes` | Write to several nodes in one request. |

### Node references

Every tool that takes a "node" accepts any of these forms:

- **Node id**: `ns=2;i=29` (XML) or `ns=4;i=29` (live) — both are understood; the server translates between namespaces automatically.
- **Browse name**: `Wind speed`.
- **Path**: `Server interface_1/NMEA Data/Wind speed`.

### Struct decoding examples

Reading the `NMEA Data` structure would normally return an `ExtensionObject` with a 14-byte binary payload:

```
Node ns=4;i=28 value: ExtensionObject(TypeId:FourByteNodeId(ns=4;i=27), Encoding:1, 14 bytes)
```

With the XML struct definitions it is decoded into readable fields:

```json
{
  "Wind speed": 12.5,
  "SpeedOverGround": 8.1,
  "BaroMeterValue": 1013.25,
  "BaroMeterOK": 1
}
```

Nested structures (e.g. `FDS_Status rooms` → `Fault` / `Disabled`, each with 16 status BOOLs) are decoded recursively. Supported field types: `BOOL`, `BYTE`, `SINT/USINT`, `INT/UINT`, `WORD`, `DINT/UDINT`, `DWORD`, `LINT/ULINT`, `LWORD`, `REAL`, `LREAL`, `CHAR`, `STRING`, `TIME/DATE`, the standard UA built-ins (`Boolean`…`Double`, `String`), arrays, and nested structs.

## Example Prompts

- "Which nodes are available?" → `get_opcua_tree` / `list_opcua_nodes`.
- "Find the node for wind speed" → `search_opcua_nodes('wind')` → resolves to the `Wind speed` variable.
- "What's the value of the wind speed node?" → Returns the current value (e.g. `12.5`).
- "What's inside 'NMEA Data'?" → decoded struct fields (see above).
- "List what's inside 'Batterij Data'" → `list_opcua_children('Batterij Data')`.
- "Set the value of 'EB V' to 24" → Writes 24 to the battery voltage node.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://192.168.0.1:4840` | Live OPC UA server to read/write. |
| `OPCUA_TREE_FILE` | `opc/testOPCTree.xml` | Path to the exported UANodeSet XML used for startup node awareness. If unset and the `opc/` folder contains exactly one `*.xml`, that one is used. |

## Installation

### Prerequisites
- Python 3.13 or higher
- An OPC UA server (e.g., a simulator or real industrial device)

### Install Dependencies
Clone the repository and install the required Python packages:

```bash
git clone https://github.com/kukapay/opcua-mcp.git
cd opcua-mcp
pip install mcp[cli] opcua cryptography
```

The project can also be run with `uv run main.py` when using [uv](https://docs.astral.sh/uv/).

### MCP Client Configuration

```json
{
 "mcpServers": {
   "opcua-mcp": {
     "command": "python",
     "args": ["path/to/opcua_mcp/main.py"],
     "env": {
        "OPCUA_SERVER_URL": "your-opc-ua-server-url",
        "OPCUA_TREE_FILE": "path/to/your/exported-tree.xml"
     }
   }
 }
}
```

## License
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.