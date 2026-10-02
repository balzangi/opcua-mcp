import json
import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, List, Dict, Any, Optional

from mcp.server.fastmcp import FastMCP, Context
from opcua import Client
from opcua import ua

from opc_tree import OpcTree, OpcNode, discover_tree_file, decode_struct_bytes, StructDecodeError

server_url = os.getenv("OPCUA_SERVER_URL", "opc.tcp://192.168.0.1:4840")

# ---------------------------------------------------------------------------
# Load the OPC UA address-space index at startup so the server is immediately
# aware of the available nodes (discovery works even without a live server).
# The tree is loaded eagerly so the model can be told about it in `instructions`.
# ---------------------------------------------------------------------------
OPC_TREE = OpcTree()
try:
    OPC_TREE.load()
    print(f"Loaded OPC UA node tree: {len(OPC_TREE.nodes)} nodes from {OPC_TREE.source}")
except Exception as e:
    print(f"Warning: could not load OPC UA node tree ({discover_tree_file()}): {e}")

_TREE_HINT = (
    "The OPC UA address space is preloaded at startup "
    f"({len(OPC_TREE.nodes)} nodes from '{OPC_TREE.source}'). "
    "You can answer node-related questions using only this cached tree, without "
    "touching the live server. Start with search_opcua_nodes or get_opcua_tree, "
    "then read_opcua_node / write_opcua_node / list_opcua_children using either a "
    "node id (e.g. 'ns=2;i=29'), a browse name (e.g. 'Wind speed'), or a path "
    "(e.g. 'Server interface_1/NMEA Data/Wind speed')."
)


# Manage the lifecycle of the OPC UA client connection
@asynccontextmanager
async def opcua_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Handle OPC UA client connection lifecycle."""
    client = Client(server_url)
    connected = False
    ns_map: Dict[int, int] = {}      # XML namespace index -> live namespace index
    live_names: Dict[str, List[str]] = {}  # lowercased browse name -> [live ids]
    try:
        # Connect to OPC UA server synchronously, wrapped in a thread for async compatibility
        await asyncio.to_thread(client.connect)
        connected = True
        print("Connected to OPC UA server")
        # Siemens / other servers often renumber namespaces (XML says ns=2 but the
        # live server exposes the same nodes as ns=4). Bridge the two by URI.
        ns_map = await asyncio.to_thread(lambda: _build_ns_map(client))
        if ns_map:
            print(f"Namespace mapping (XML -> live): {ns_map}")
        else:
            print("No URI match for namespaces; falling back to name-based lookup.")
            live_names = await asyncio.to_thread(lambda: _build_live_name_index(client))
    except Exception as e:
        print(f"Failed to connect to OPC UA server at {server_url}: {e}")

    base_ctx = {
        "opc_tree": OPC_TREE,
        "tree_source": OPC_TREE.source,
        "ns_map": ns_map,
        "live_names": live_names,
        "opcua_client": client if connected else None,
        "connection_error": "" if connected else "not connected to live server",
    }
    try:
        yield base_ctx
    finally:
        if connected:
            try:
                await asyncio.to_thread(client.disconnect)
                print("Disconnected from OPC UA server")
            except Exception as e:
                print(f"Error disconnecting from OPC UA server: {e}")


def _build_ns_map(client: Client) -> Dict[int, int]:
    """Map XML namespace indices to the live server's indices by URI."""
    live_uris = client.get_namespace_array()
    ns_map: Dict[int, int] = {}
    for xml_index, uri in OPC_TREE.namespaces.items():
        if xml_index == 0:
            continue
        try:
            live_index = live_uris.index(uri)
        except ValueError:
            continue
        ns_map[xml_index] = live_index
    return ns_map


def _build_live_name_index(client: Client, max_nodes: int = 3000, max_depth: int = 12) -> Dict[str, List[str]]:
    """Browse the live address space and index node ids by browse name.

    Used when the namespace URIs of the XML export and the live server do not
    match, so node discovery still works (names are more stable than numbers).
    """
    index: Dict[str, List[str]] = {}
    visited: set = set()

    def walk(node, depth: int) -> None:
        cid = node.nodeid.to_string()
        if cid in visited or len(visited) > max_nodes or depth > max_depth:
            return
        visited.add(cid)
        try:
            children = node.get_children()
        except Exception:
            return
        for child in children:
            try:
                browse_name = child.get_browse_name()
                key = browse_name.Name.strip().lower()
                if key:
                    index.setdefault(key, []).append(child.nodeid.to_string())
            except Exception:
                pass
            walk(child, depth + 1)

    walk(client.get_objects_node(), 0)
    print(f"Live name index built: {len(index)} unique names")
    return index


_OPCUA_INSTRUCTIONS = (
    "OPC UA control server. You have access to live read/write tools and to a "
    f"node catalogue preloaded at startup ({_TREE_HINT}) "
    "When the user asks to read, write or browse a node, use the node catalogue "
    "tools to resolve a friendly name/path down to a concrete node id before "
    "calling the live tools."
)


# Create an MCP server instance
mcp = FastMCP(
    "OPCUA-Control",
    lifespan=opcua_lifespan,
    host="127.0.0.1",
    port=9000,
    instructions=_OPCUA_INSTRUCTIONS,
)


# --------------------------------------------------------------------------
# Context helpers
# --------------------------------------------------------------------------
def _lifespan_parts(ctx: Context) -> Dict[str, Any]:
    return ctx.request_context.lifespan_context


def _opc_tree(ctx: Context) -> OpcTree:
    return _lifespan_parts(ctx).get("opc_tree") or OPC_TREE


def _json(obj: Any, indent: int = 2) -> str:
    return json.dumps(obj, indent=indent, default=str)


def _remap(ctx: Context, data: Any) -> Any:
    """Translate XML namespaces to the live ones when building tool output,
    so the ids a client sees are the ids it can actually read/write."""
    ns_map = _lifespan_parts(ctx).get("ns_map") or {}
    if not ns_map:
        return data
    if isinstance(data, dict):
        out: Dict[str, Any] = {}
        for key, value in data.items():
            if key in ("node_id", "parent_id"):
                out[key] = _apply_ns_map(ns_map, value)
            elif key == "children" and isinstance(value, list):
                out[key] = [_remap(ctx, item) for item in value]
            else:
                out[key] = value
        return out
    if isinstance(data, list):
        return [_remap(ctx, item) for item in data]
    return data


def _apply_ns_map(ns_map: Dict[int, int], node_id: str) -> str:
    """Translate an XML node id to the live server's namespace index."""
    if not ns_map:
        return node_id
    match = re.match(r"^ns=(\d+);i=(\d+)$", node_id)
    if not match:
        return node_id
    xml_ns = int(match.group(1))
    mapped = ns_map.get(xml_ns)
    if mapped is None:
        return node_id
    return f"ns={mapped};i={match.group(2)}"


def _resolve_tree(ctx: Context, reference: str) -> str:
    """Resolve a friendly reference (browse name / path) to a concrete node id.

    Resolution order:
      1. resolve against the preloaded XML tree;
      2. if the live server renumbers namespaces, translate the XML id to the
         live namespace via the URI map;
      3. otherwise, fall back to a name lookup against the live address space.
    """
    tree = _opc_tree(ctx)
    node = tree.resolve(reference.strip()) if reference else None
    ctx_data = _lifespan_parts(ctx)

    xml_or_live = node.node_id if node else reference.strip()

    mapped = _apply_ns_map(ctx_data.get("ns_map"), xml_or_live)
    if mapped != xml_or_live:
        return mapped

    # Namespace URI mapping was not available -> try live name lookup.
    live_names = ctx_data.get("live_names") or {}
    if node is not None:
        ids = live_names.get(node.name.lower(), [])
        if ids:
            for cid in ids:
                if not cid.startswith("ns=0;"):
                    return cid
            return ids[0]
    return xml_or_live


def _find_struct(
    ctx: Context, extension_object: ua.ExtensionObject, resolved_node: Optional[OpcNode]
) -> Optional[Any]:
    """Locate the XML struct definition matching an ExtensionObject value."""
    tree = _opc_tree(ctx)
    type_id = extension_object.TypeId.to_string()

    # 1) exact NodeId match
    if type_id in tree.struct_types:
        return tree.struct_types[type_id]
    # 2) the node we read knows its data type
    if resolved_node is not None and resolved_node.data_type in tree.struct_types:
        return tree.struct_types[resolved_node.data_type]

    # 3) the TypeId is usually the *encoding* node; its parent is the data type.
    client = _lifespan_parts(ctx).get("opcua_client")
    if client is not None:
        nodes_to_try: List[Any] = []
        try:
            nodes_to_try.append(client.get_node(type_id))
        except Exception:
            pass
        for candidate in list(nodes_to_try):
            try:
                nodes_to_try.append(candidate.get_parent())
            except Exception:
                pass
        for candidate in nodes_to_try:
            try:
                browse_name = candidate.get_browse_name()
                struct_type = tree.struct_by_name.get(browse_name.Name.strip().lower())
                if struct_type is not None:
                    return struct_type
            except Exception:
                continue
    return None


def _format_value(
    ctx: Context, value: Any, resolved_node: Optional[OpcNode] = None
) -> Any:
    """Format a raw read value; decode struct ExtensionObjects into readable fields."""
    if isinstance(value, ua.Variant):
        value = value.Value
    if isinstance(value, ua.ExtensionObject):
        struct_type = _find_struct(ctx, value, resolved_node)
        if struct_type is None:
            return repr(value)
        try:
            decoded = decode_struct_bytes(value.Body, struct_type, _opc_tree(ctx))
            return _json(decoded)
        except StructDecodeError as e:
            return f"{value!r} (struct '{struct_type.name}' decode failed: {e})"
        except Exception as e:
            return f"{value!r} (struct '{struct_type.name}' decode failed: {type(e).__name__}: {e})"
    return value


# --------------------------------------------------------------------------
# Node catalogue tools (work from the preloaded tree, no live server needed)
# --------------------------------------------------------------------------
@mcp.tool()
def search_opcua_nodes(query: str, ctx: Context) -> str:
    """
    Search the preloaded OPC UA node catalogue for nodes matching a query.

    Searches browse names, node ids and Siemens mapping paths (e.g. a DB name).
    Useful first step to discover which node the user means.

    Parameters:
        query (str): Text to search for (e.g. 'wind', 'batterij', 'DB14', 'ns=2;i=29').

    Returns:
        str: JSON list of matching nodes with their node id, browse name, class
             and data type, ranked by relevance.
    """
    tree = _opc_tree(ctx)
    results = tree.search(query, limit=25)
    if not results:
        return f"No nodes found matching '{query}' in the preloaded tree."
    return _json(
        {
            "query": query,
            "count": len(results),
            "nodes": _remap(ctx, [node.summary() for node in results]),
        }
    )


@mcp.tool()
def list_opcua_nodes(
    node_class: str = "",
    parent: str = "",
    query: str = "",
    limit: int = 100,
    offset: int = 0,
    ctx: Context = None,
) -> str:
    """
    List nodes from the preloaded OPC UA catalogue, with optional filters.

    Parameters:
        node_class (str): Optional filter: 'Variable', 'Object', 'DataType',
                          'ObjectType'. Empty means all.
        parent (str): Optional filter: node id/browse name/path of the parent.
        query (str): Optional substring filter on name/node id/S7 path.
        limit (int): Maximum number of nodes to return (default 100).
        offset (int): Pagination offset.

    Returns:
        str: JSON list of matching nodes.
    """
    tree = _opc_tree(ctx)
    nodes = tree.list_nodes(
        node_class=node_class or None,
        parent=parent or None,
        query=query or None,
        limit=limit,
        offset=offset,
    )
    return _json(
        {
            "total_matching": len(nodes) if len(nodes) < limit else f"at least {limit}",
            "count": len(nodes),
            "nodes": _remap(ctx, [node.summary() for node in nodes]),
        }
    )


@mcp.tool()
def get_opcua_node_info(reference: str, ctx: Context) -> str:
    """
    Get detailed information about a single OPC UA node from the preloaded tree.

    Parameters:
        reference (str): Node id (e.g. 'ns=2;i=29'), browse name (e.g. 'Wind speed'),
                         or path (e.g. 'Server interface_1/NMEA Data/Wind speed').

    Returns:
        str: JSON object with node metadata, parent, data type, writability,
             Siemens mapping path and direct children.
    """
    tree = _opc_tree(ctx)
    node = tree.resolve(reference.strip())
    if node is None:
        candidates = tree.search(reference.strip(), limit=8)
        if candidates:
            return _json(
                {
                    "error": f"Could not uniquely resolve '{reference}'",
                    "candidates": _remap(ctx, [n.summary() for n in candidates]),
                }
            )
        return f"Node '{reference}' not found in the preloaded tree."
    return _json(_remap(ctx, node.to_dict(tree)))


@mcp.tool()
def list_opcua_children(reference: str, ctx: Context) -> str:
    """
    List the child nodes of a node using the preloaded OPC UA tree.

    This works even when the live OPC UA server is unreachable.

    Parameters:
        reference (str): Node id, browse name, or path of the parent
                         (e.g. 'Server interface_1' or 'ns=2;i=1').

    Returns:
        str: JSON list of child nodes with node id, browse name and class.
    """
    tree = _opc_tree(ctx)
    node = tree.resolve(reference.strip())
    if node is None:
        return f"Node '{reference}' not found in the preloaded tree."
    children = tree.children(node.node_id)
    return _json(
        {
            "parent": _remap(ctx, {"node_id": node.node_id, "browse_name": node.browse_name}),
            "child_count": len(children),
            "children": _remap(ctx, [child.summary() for child in children]),
        }
    )


@mcp.tool()
def get_opcua_tree(root: str = "ServerInterfaces", depth: int = 3, max_children: int = 50, ctx: Context = None) -> str:
    """
    Return a nested view of the OPC UA node hierarchy (preloaded tree).

    Parameters:
        root (str): Starting node: node id, browse name, or path. Default
                    'ServerInterfaces' (top level of the Siemens server).
        depth (int): How many levels deep to expand.
        max_children (int): Cap on how many children are shown per node.

    Returns:
        str: JSON tree with node ids, browse names and node classes.
    """
    tree = _opc_tree(ctx)

    if root:
        head = tree.resolve(root.strip())
        if head is None:
            return f"Root '{root}' not found in the preloaded tree."
        heads = [head]
    else:
        heads = [tree.nodes[nid] for nid in tree.roots]

    def walk(node, level):
        entry = {
            "node_id": node.node_id,
            "browse_name": node.browse_name,
            "class": node.node_class,
        }
        if node.value is not None:
            entry["value"] = node.value
        children = [tree.nodes[c] for c in node.children if c in tree.nodes]
        if level < depth and children:
            entry["children"] = [walk(child, level + 1) for child in children[:max_children]]
            if len(children) > max_children:
                entry["children_truncated"] = len(children) - max_children
        elif children:
            entry["child_count"] = len(children)
        return entry

    return _json(
        _remap(ctx, {"root": heads[0].node_id if heads else None, "tree": [walk(h, 1) for h in heads]})
    )


@mcp.tool()
def reload_opcua_tree(path: str = "", ctx: Context = None) -> str:
    """
    Reload the OPC UA node catalogue from a UANodeSet XML file.

    Parameters:
        path (str): Path to the XML file. Empty means the default/discovered one.

    Returns:
        str: JSON confirmation with the loaded node count.
    """
    tree = _opc_tree(ctx)
    try:
        tree.load(path or None)
        return _json(
            {
                "status": "ok",
                "source": tree.source,
                "node_count": len(tree.nodes),
            }
        )
    except Exception as e:
        return f"Error reloading tree from '{path or discover_tree_file()}': {e}"


# --------------------------------------------------------------------------
# Live tools (need a connection; node ids can be resolved via the catalogue)
# --------------------------------------------------------------------------
# Tool: Read the value of an OPC UA node
@mcp.tool()
def read_opcua_node(node_id: str, ctx: Context) -> str:
    """
    Read the value of a specific OPC UA node.

    Parameters:
        node_id (str): The node to read. Can be a node id ('ns=2;i=29'), a browse
                       name ('Wind speed'), or a path ('Server interface_1/NMEA
                       Data/Wind speed').

    Returns:
        str: The value of the node. Scalar nodes return their value directly;
             structure-typed nodes (e.g. 'NMEA Data') are decoded into readable
             JSON fields.
    """
    ctx_data = _lifespan_parts(ctx)
    client = ctx_data.get("opcua_client")
    if client is None:
        return f"Error: Not connected to OPC UA server. {ctx_data.get('connection_error', '')}"
    tree = _opc_tree(ctx)
    resolved = _resolve_tree(ctx, node_id)
    node = client.get_node(resolved)
    value = _format_value(ctx, node.get_value(), tree.resolve(node_id.strip()))
    return f"Node {resolved} value: {value}"


# Tool: Write a value to an OPC UA node
@mcp.tool()
def write_opcua_node(node_id: str, value: str, ctx: Context) -> str:
    """
    Write a value to a specific OPC UA node.

    Parameters:
        node_id (str): The node to write to. Can be a node id ('ns=2;i=29'), a
                       browse name ('Wind speed'), or a path.
        value (str): The value to write to the node. Will be converted based on node type.

    Returns:
        str: A message indicating success or failure of the write operation.
    """
    ctx_data = _lifespan_parts(ctx)
    client = ctx_data.get("opcua_client")
    if client is None:
        return f"Error: Not connected to OPC UA server. {ctx_data.get('connection_error', '')}"
    resolved = _resolve_tree(ctx, node_id)
    node = client.get_node(resolved)
    try:
        # Convert value based on the node's current type
        current_value = node.get_value()
        python_typed_value = value
        if isinstance(current_value, float):
            python_typed_value = float(value)
        elif isinstance(current_value, int):
            python_typed_value = int(value)
        # Create a DataValue without SourceTimestamp (some servers reject timestamps on write)
        variant_type = node.get_data_type_as_variant_type()
        variant = ua.Variant(python_typed_value, variant_type)
        datavalue = ua.DataValue(variant)
        datavalue.SourceTimestamp = None
        datavalue.ServerTimestamp = None
        node.set_attribute(ua.AttributeIds.Value, datavalue)
        return f"Successfully wrote {value} to node {resolved}"
    except Exception as e:
        return f"Error writing to node {resolved}: {str(e)}"


@mcp.tool()
def browse_opcua_node_children(node_id: str, ctx: Context) -> str:
    """
    Browse the children of a specific OPC UA node.

    Uses the preloaded node tree when possible; falls back to the live server
    for nodes not present in the tree.

    Parameters:
        node_id (str): The node to browse. Can be a node id, browse name, or path
                       (e.g. 'ns=2;i=1' or 'Server interface_1').

    Returns:
        str: JSON string representation of the child nodes, including their
             NodeId and BrowseName. Returns an error message on failure.
    """
    ctx_data = _lifespan_parts(ctx)
    client = ctx_data.get("opcua_client")
    tree = _opc_tree(ctx)

    resolved = _resolve_tree(ctx, node_id)

    # 1) Preloaded tree (works offline)
    cached = tree.resolve(resolved)
    if cached is not None and cached.children:
        children = tree.children(cached.node_id)
        return _json(
            {
                "source": "preloaded_tree",
                "parent": _remap(ctx, {"node_id": cached.node_id, "browse_name": cached.browse_name}),
                "children": _remap(ctx, [child.summary() for child in children]),
            }
        )

    # 2) Live server fallback
    if client is None:
        return f"Error: Not connected to OPC UA server. {ctx_data.get('connection_error', '')}"
    try:
        node = client.get_node(resolved)
        live_children = node.get_children()

        children_info = []
        for child in live_children:
            try:
                browse_name = child.get_browse_name()
                children_info.append(
                    {
                        "node_id": child.nodeid.to_string(),
                        "browse_name": f"{browse_name.NamespaceIndex}:{browse_name.Name}",
                    }
                )
            except Exception as e:
                children_info.append(
                    {
                        "node_id": child.nodeid.to_string(),
                        "browse_name": f"Error getting name: {e}",
                    }
                )
        return _json({"source": "live_server", "parent": resolved, "children": children_info})
    except Exception as e:
        return f"Error Browse children of node {resolved}: {str(e)}"


@mcp.tool()
def read_multiple_opcua_nodes(node_ids: List[str], ctx: Context) -> str:
    """
    Read the values of multiple OPC UA nodes in a single request.

    Parameters:
        node_ids (List[str]): A list of nodes to read. Each entry can be a node id
                              ('ns=2;i=29'), browse name ('Wind speed'), or path.

    Returns:
        str: A string representation of a dictionary mapping node IDs to their
             values, or an error message.
    """
    ctx_data = _lifespan_parts(ctx)
    client = ctx_data.get("opcua_client")
    if client is None:
        return f"Error: Not connected to OPC UA server. {ctx_data.get('connection_error', '')}"
    tree = _opc_tree(ctx)
    resolved_ids = [_resolve_tree(ctx, nid) for nid in node_ids]
    resolved_nodes = [tree.resolve(nid.strip()) for nid in node_ids]
    try:
        nodes_to_read = [client.get_node(nid) for nid in resolved_ids]
        values = []
        # Iterate over each node in nodes_to_read
        for node, resolved_node in zip(nodes_to_read, resolved_nodes):
            try:
                # Get the value of the current node
                value = _format_value(ctx, node.get_value(), resolved_node)
                # Append the value to the values list
                values.append(value)
            except Exception as e:
                # In case of an error, append the error message
                values.append(f"Error reading node {node.nodeid.to_string()}: {str(e)}")

        # Map node IDs to their corresponding values
        results = {
            node.nodeid.to_string(): value for node, value in zip(nodes_to_read, values)
        }

        return f"Read multiple nodes values: {results!r}"

    except ua.UaError as e:
        status_name = e.code_as_name() if hasattr(e, "code_as_name") else "Unknown"
        status_code_hex = f"0x{e.code:08X}" if hasattr(e, "code") else "N/A"
        return f"Error reading multiple nodes {resolved_ids}: OPC UA Error - Status: {status_name} ({status_code_hex})"
    except Exception as e:
        return f"Error reading multiple nodes {resolved_ids}: {type(e).__name__} - {str(e)}"


@mcp.tool()
def write_multiple_opcua_nodes(
    nodes_to_write: List[Dict[str, Any]], ctx: Context
) -> str:
    """
    Write values to multiple OPC UA nodes in a single request.

    Parameters:
        nodes_to_write (List[Dict[str, Any]]): A list of dictionaries, where each
                                               dictionary contains 'node_id' (str,
                                               may be a node id, browse name or path)
                                               and 'value' (Any).
                                               Example: [{'node_id': 'ns=2;i=2', 'value': 10.5},
                                                         {'node_id': 'Wind speed', 'value': 12.0}]

    Returns:
        str: A message indicating the success or failure of the write operation.
             Returns status codes for each write attempt.
    """
    ctx_data = _lifespan_parts(ctx)
    client = ctx_data.get("opcua_client")
    if client is None:
        return f"Error: Not connected to OPC UA server. {ctx_data.get('connection_error', '')}"

    node_ids_for_error_msg = [
        _resolve_tree(ctx, item.get("node_id", "unknown_node")) for item in nodes_to_write
    ]

    try:
        nodes = [client.get_node(_resolve_tree(ctx, item["node_id"])) for item in nodes_to_write]

        # Iterate over nodes and values to set each value individually
        status_report = []
        for node, item in zip(nodes, nodes_to_write):
            try:
                # Get the node's current value to determine the expected type
                current_value = node.get_value()
                python_value = item["value"]
                if isinstance(current_value, float):
                    python_value = float(item["value"])
                elif isinstance(current_value, int):
                    python_value = int(item["value"])
                # Create a DataValue without SourceTimestamp (some servers reject timestamps on write)
                variant_type = node.get_data_type_as_variant_type()
                variant = ua.Variant(python_value, variant_type)
                datavalue = ua.DataValue(variant)
                datavalue.SourceTimestamp = None
                datavalue.ServerTimestamp = None
                node.set_attribute(ua.AttributeIds.Value, datavalue)

                status_report.append(
                    {
                        "node_id": node.nodeid.to_string(),
                        "value_written": item["value"],
                        "status": "Success",
                    }
                )
            except Exception as e:
                return f"Error writing to node {node}: {str(e)}"
        # Return the status report
        return f"Write multiple nodes results: {status_report!r}"

    except ua.UaError as e:
        status_name = e.code_as_name() if hasattr(e, "code_as_name") else "Unknown"
        status_code_hex = f"0x{e.code:08X}" if hasattr(e, "code") else "N/A"
        return f"Error writing multiple nodes {node_ids_for_error_msg}: OPC UA Error - Status: {status_name} ({status_code_hex})"
    except Exception as e:
        return f"Error writing multiple nodes {node_ids_for_error_msg}: {type(e).__name__} - {str(e)}"


# Run the server (Streamable HTTP transport)
if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    starlette_app = mcp.streamable_http_app()
    # Add CORS for browser-based MCP clients
    starlette_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    uvicorn.run(starlette_app, host=mcp.settings.host, port=mcp.settings.port)