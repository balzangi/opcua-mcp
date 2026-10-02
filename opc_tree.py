"""In-memory OPC UA address-space index loaded from a ``UANodeSet`` XML export.

The MCP server loads this file at startup so that it is immediately aware of
every node the server exposes *before* (or even without) a live connection.
That lets a client discover nodes by a friendly node id, browse name or path
and then read, write or browse them.

The parser is intentionally dependency free (stdlib ``xml.etree`` only) and
tolerant of partially specified node sets.
"""

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

UANS = "{http://opcfoundation.org/UA/2011/03/UANodeSet.xsd}"
UATYPES = "{http://opcfoundation.org/UA/2008/02/Types.xsd}"
SI = "{http://www.siemens.com/OPCUA/2017/SimaticNodeSetExtensions}"

#: Default location of the exported NodeSet, relative to this file.
DEFAULT_TREE_FILE = "opc/testOPCTree.xml"

#: Reference types that build the browse (parent/child) hierarchy.
HIERARCHICAL_REFERENCES = {
    "Organizes",
    "HasComponent",
    "HasProperty",
    "HasChild",
    "HasSubtype",
    "HasEncoding",
    "HasEventSource",
    "HasNotifier",
    "HasOrderedComponent",
    "HasInterface",
    "HasAddIn",
}

#: Standard node ids for hierarchical references, used when the NodeSet does
#: not declare an alias and stores the raw node id instead of the name.
REFERENCE_ID_TO_NAME = {
    "i=35": "Organizes",
    "i=47": "HasComponent",
    "i=46": "HasProperty",
    "i=34": "HasChild",
    "i=45": "HasSubtype",
    "i=38": "HasEncoding",
    "i=36": "HasEventSource",
    "i=48": "HasNotifier",
    "i=49": "HasOrderedComponent",
    "i=17603": "HasInterface",
    "i=17604": "HasAddIn",
}

_INTEGER_TYPES = {
    "Byte",
    "SByte",
    "Int16",
    "UInt16",
    "Int32",
    "UInt32",
    "Int64",
    "UInt64",
}
_FLOAT_TYPES = {"Float", "Double", "Decimal"}


@dataclass
class OpcNode:
    """A single node from the exported address space."""

    node_id: str
    browse_name: str
    name: str
    namespace_index: int
    namespace_uri: str
    node_class: str
    display_name: str = ""
    data_type: str = ""
    access_level: Optional[int] = None
    value: Any = None
    parent_id: Optional[str] = None
    s7_mapping: str = ""
    fields: list[str] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    children: list[str] = field(default_factory=list)

    @property
    def is_writable(self) -> bool:
        """AccessLevel bit 1 (0x02) means the value can be written."""
        return bool(self.access_level and (self.access_level & 0x02))

    def summary(self) -> dict[str, Any]:
        """Compact representation used in searches / listings."""
        data: dict[str, Any] = {
            "node_id": self.node_id,
            "browse_name": self.browse_name,
            "name": self.name,
            "class": self.node_class,
        }
        if self.data_type:
            data["data_type"] = self.data_type
        if self.access_level is not None:
            data["access_level"] = self.access_level
            data["writable"] = self.is_writable
        if self.s7_mapping:
            data["s7_path"] = self.s7_mapping
        if self.value is not None:
            data["value"] = self.value
        if self.children:
            data["child_count"] = len(self.children)
        return data

    def to_dict(self, tree: "OpcTree") -> dict[str, Any]:
        """Full representation including children and struct fields."""
        data = self.summary()
        data["namespace_index"] = self.namespace_index
        data["namespace_uri"] = self.namespace_uri
        if self.parent_id:
            data["parent_id"] = self.parent_id
        if self.display_name and self.display_name != self.name:
            data["display_name"] = self.display_name
        if self.fields:
            data["fields"] = self.fields
        data["children"] = [
            {
                "node_id": tree.nodes[cid].node_id,
                "browse_name": tree.nodes[cid].browse_name,
                "class": tree.nodes[cid].node_class,
            }
            for cid in self.children
            if cid in tree.nodes
        ]
        return data


@dataclass
class StructField:
    """One field of a structure data type (from a ``<Definition>`` block)."""

    name: str
    data_type: str  # e.g. "REAL", "i=6", or a NodeId of another structure
    value_rank: int = -1  # -1 = scalar, >= 0 = array


@dataclass
class StructType:
    """A structure data type with its ordered binary-encoded fields."""

    node_id: str
    browse_name: str
    name: str
    fields: list[StructField]


class StructDecodeError(Exception):
    """Raised when an ``ExtensionObject`` payload cannot be decoded."""


# Siemens S7 data types -> (size in bytes, struct format char, python kind)
_SIEMENS_TYPES: dict[str, tuple[int, str, str]] = {
    "BOOL": (1, "B", "bool"),
    "BYTE": (1, "B", "int"),
    "USINT": (1, "B", "int"),
    "SINT": (1, "b", "int"),
    "CHAR": (1, "b", "int"),
    "WORD": (2, "H", "int"),
    "UINT": (2, "H", "int"),
    "INT": (2, "h", "int"),
    "DWORD": (4, "I", "int"),
    "UDINT": (4, "I", "int"),
    "DINT": (4, "i", "int"),
    "REAL": (4, "f", "float"),
    "TIME": (4, "I", "int"),
    "DATE": (4, "I", "int"),
    "TOD": (4, "I", "int"),
    "LWORD": (8, "Q", "int"),
    "ULINT": (8, "Q", "int"),
    "LINT": (8, "q", "int"),
    "LREAL": (8, "d", "float"),
    "LTIME": (8, "Q", "int"),
    "LDT": (8, "Q", "int"),
    "STRING": (-1, "str", "str"),
    "WSTRING": (-1, "str", "str"),
}

# Standard OPC UA built-in data types -> same spec tuples.
_UA_TYPES: dict[str, tuple[int, str, str]] = {
    "i=1": (1, "B", "bool"),  # Boolean
    "i=2": (1, "b", "int"),  # SByte
    "i=3": (1, "B", "int"),  # Byte
    "i=4": (2, "h", "int"),  # Int16
    "i=5": (2, "H", "int"),  # UInt16
    "i=6": (4, "i", "int"),  # Int32
    "i=7": (4, "I", "int"),  # UInt32
    "i=8": (8, "q", "int"),  # Int64
    "i=9": (8, "Q", "int"),  # UInt64
    "i=10": (4, "f", "float"),  # Float
    "i=11": (8, "d", "float"),  # Double
    "i=12": (-1, "str", "str"),  # String
}


class _ByteReader:
    """Minimal binary reader for OPC UA struct payloads."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise StructDecodeError(
                f"struct payload too short: needed {size} bytes at offset "
                f"{self.pos}, only {len(self.data)} available"
            )
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def uint32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def skip_node_id(self) -> None:
        """Skip a binary NodeId (used for nested ExtensionObjects)."""
        encoding = self.take(1)[0]
        kind = (encoding >> 2) & 0x0F
        ns_size = encoding & 0x03
        if ns_size == 1:
            self.take(1)
        elif ns_size == 2:
            self.take(2)
        elif ns_size == 3:
            self.take(4)
        if kind == 0x00:
            self.take(1)  # TwoByte
        elif kind == 0x01:
            self.take(2)  # FourByte
        elif kind == 0x02:
            self.take(4)  # Numeric
        elif kind == 0x03:
            self.take(self.uint32())  # String
        elif kind == 0x04:
            self.take(16)  # Guid
        elif kind == 0x05:
            self.take(self.uint32())  # ByteString


def _decode_string(reader: _ByteReader) -> Optional[str]:
    length = reader.uint32()
    if length == 0xFFFFFFFF or length == 0:
        return None if length == 0xFFFFFFFF else ""
    raw = reader.take(length)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _decode_scalar(
    reader: _ByteReader,
    struct_type: Optional[StructType],
    spec: Optional[tuple[int, str, str]],
    tree: "OpcTree",
) -> Any:
    if struct_type is not None:
        # Nested structure: ExtensionObject header (NodeId + encoding byte) + body
        reader.skip_node_id()
        encoding = reader.take(1)[0]
        if encoding != 1:  # 1 == HasBinaryBody (only binary encoding is supported)
            raise StructDecodeError(f"unsupported nested ExtensionObject encoding: {encoding}")
        return _decode_struct(reader, struct_type, tree)
    if spec is None:
        raise StructDecodeError("unknown field data type, cannot decode")
    size, fmt, kind = spec
    if fmt == "str":
        return _decode_string(reader)
    value = struct.unpack("<" + fmt, reader.take(size))[0]
    if kind == "bool":
        return bool(value)
    if fmt in ("f", "d"):
        return value
    return int(value)


def _decode_field(reader: _ByteReader, entry: StructField, tree: "OpcTree") -> Any:
    target = tree.struct_types.get(entry.data_type)
    spec = None if target is not None else _SIEMENS_TYPES.get(entry.data_type) or _UA_TYPES.get(entry.data_type)

    if entry.value_rank >= 0:
        count = reader.uint32()
        return [_decode_scalar(reader, target, spec, tree) for _ in range(count)]
    return _decode_scalar(reader, target, spec, tree)


def _decode_struct(reader: _ByteReader, struct_type: StructType, tree: "OpcTree") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in struct_type.fields:
        result[entry.name] = _decode_field(reader, entry, tree)
    return result


def decode_struct_bytes(body: bytes, struct_type: StructType, tree: "OpcTree") -> dict[str, Any]:
    """Decode the binary payload of an ``ExtensionObject`` into typed fields."""
    return _decode_struct(_ByteReader(body), struct_type, tree)


def discover_tree_file() -> str:
    """Resolve the NodeSet XML to load (env override, then ``opc/*.xml``)."""
    env_path = os.getenv("OPCUA_TREE_FILE")
    if env_path:
        return env_path

    opc_dir = Path(__file__).resolve().parent / "opc"
    if opc_dir.is_dir():
        candidates = sorted(opc_dir.glob("*.xml"))
        if len(candidates) == 1:
            return str(candidates[0])
    return str(Path(__file__).resolve().parent / DEFAULT_TREE_FILE)


class OpcTree:
    """Searchable index over the nodes of a ``UANodeSet`` export."""

    def __init__(self) -> None:
        self.nodes: dict[str, OpcNode] = {}
        self.namespaces: dict[int, str] = {0: "http://opcfoundation.org/UA/"}
        self.roots: list[str] = []
        self.source: Optional[str] = None
        #: structure data types by NodeId and by (lowercase) simple name
        self.struct_types: dict[str, StructType] = {}
        self.struct_by_name: dict[str, StructType] = {}

    # ------------------------------------------------------------------ load
    @classmethod
    def from_file(cls, path: Optional[str] = None) -> "OpcTree":
        tree = cls()
        tree.load(path)
        return tree

    def clear(self) -> None:
        self.nodes.clear()
        self.namespaces = {0: "http://opcfoundation.org/UA/"}
        self.roots.clear()
        self.source = None
        self.struct_types.clear()
        self.struct_by_name.clear()

    def load(self, path: Optional[str] = None) -> "OpcTree":
        """(Re)load the address space from *path* (or the discovered default)."""
        self.clear()
        path = path or discover_tree_file()
        self.source = str(path)
        root = ET.parse(path).getroot()

        ns_uris = root.find(f"{UANS}NamespaceUris")
        if ns_uris is not None:
            for index, uri in enumerate(ns_uris.findall(f"{UANS}Uri"), start=1):
                self.namespaces[index] = (uri.text or "").strip()

        aliases: dict[str, str] = {}
        for alias in root.findall(f"{UANS}Aliases/{UANS}Alias"):
            name = alias.attrib.get("Alias", "")
            if name:
                aliases[name] = (alias.text or "").strip()

        # Pass 1: materialise every node.
        for element in root:
            if not element.tag.startswith(f"{UANS}UA"):
                continue
            node_class = element.tag[len(UANS) + 2 :]
            node = self._parse_node(element, node_class, aliases)
            if node.node_id:
                self.nodes[node.node_id] = node

        # Pass 1b: register structure data types (they own the field layout
        # needed to decode the binary payloads returned for struct-typed nodes).
        for node in self.nodes.values():
            if node.node_class != "DataType":
                continue
            if not node.fields:
                continue  # only data types carrying a <Definition> block
            struct_type = StructType(
                node_id=node.node_id,
                browse_name=node.browse_name,
                name=node.name,
                fields=[StructField(name, data_type, -1) for name, data_type in [f.split(":", 1) if ":" in f else (f, "") for f in node.fields]],
            )
            self.struct_types[node.node_id] = struct_type
            self.struct_by_name.setdefault(node.name.lower(), struct_type)

        # Pass 2: build parent/child links from forward hierarchical refs.
        for node in self.nodes.values():
            for reference in node.references:
                if not reference["is_forward"]:
                    continue
                if reference["reference_type"] not in HIERARCHICAL_REFERENCES:
                    continue
                target = self.nodes.get(reference["target_id"])
                if target is None:
                    continue
                if target.node_id not in node.children:
                    node.children.append(target.node_id)
                # An explicit ParentNodeId wins; otherwise infer it.
                if target.parent_id is None:
                    target.parent_id = node.node_id

        self.roots = [n.node_id for n in self.nodes.values() if n.parent_id is None]
        return self

    def _parse_node(
        self, element: ET.Element, node_class: str, aliases: dict[str, str]
    ) -> OpcNode:
        node_id = element.attrib.get("NodeId", "")
        browse_name = element.attrib.get("BrowseName", "")
        ns_index, name = self._split_browse_name(browse_name)

        access = element.attrib.get("AccessLevel")
        node = OpcNode(
            node_id=node_id,
            browse_name=browse_name,
            name=name,
            namespace_index=ns_index,
            namespace_uri=self.namespaces.get(ns_index, ""),
            node_class=node_class,
            display_name=(element.findtext(f"{UANS}DisplayName") or name),
            data_type=element.attrib.get("DataType", ""),
            access_level=int(access) if access else None,
            parent_id=element.attrib.get("ParentNodeId") or None,
        )

        for reference in element.findall(f"{UANS}References/{UANS}Reference"):
            ref_type = reference.attrib.get("ReferenceType", "")
            # Aliases map a friendly name -> node id; reverse them so raw ids
            # (e.g. "i=47") can be normalized back to a hierarchical type name.
            alias_names = {value: key for key, value in aliases.items()}
            ref_type = alias_names.get(ref_type, ref_type)
            ref_type = REFERENCE_ID_TO_NAME.get(ref_type, ref_type)
            node.references.append(
                {
                    "reference_type": ref_type,
                    "target_id": (reference.text or "").strip(),
                    "is_forward": reference.attrib.get("IsForward", "true").lower()
                    == "true",
                }
            )

        value_element = element.find(f"{UANS}Value")
        if value_element is not None and len(value_element):
            node.value = self._parse_value(value_element[0])

        mapping = element.find(f".//{SI}VariableMapping")
        if mapping is not None and mapping.text:
            node.s7_mapping = mapping.text.strip()

        for fld in element.findall(f"{UANS}Definition/{UANS}Field"):
            fname = fld.attrib.get("Name")
            if not fname:
                continue
            ftype = fld.attrib.get("DataType", "")
            node.fields.append(f"{fname}:{ftype}" if ftype else fname)

        return node

    @staticmethod
    def _split_browse_name(browse_name: str) -> tuple[int, str]:
        if ":" in browse_name:
            prefix, rest = browse_name.split(":", 1)
            if prefix.isdigit():
                return int(prefix), rest
        return 0, browse_name

    @staticmethod
    def _parse_value(element: ET.Element) -> Any:
        tag = element.tag.split("}")[-1]
        text = (element.text or "").strip()
        if text == "":
            return None
        try:
            if tag == "Boolean":
                return text.lower() == "true"
            if tag in _INTEGER_TYPES:
                return int(text)
            if tag in _FLOAT_TYPES:
                return float(text)
        except ValueError:
            return text
        return text

    # --------------------------------------------------------------- lookups
    def _named(self, reference: str) -> list[OpcNode]:
        key = reference.strip().lower()
        return [
            node
            for node in self.nodes.values()
            if node.browse_name.lower() == key or node.name.lower() == key
        ]

    def search(self, query: str, limit: int = 20) -> list[OpcNode]:
        """Rank nodes by how well they match *query* (id/name/path/S7 mapping)."""
        if not query:
            return []
        needle = query.strip().lower()
        scored: list[tuple[int, int, OpcNode]] = []
        for position, node in enumerate(self.nodes.values()):
            haystacks = (
                node.name.lower(),
                node.browse_name.lower(),
                node.node_id.lower(),
                node.s7_mapping.lower(),
            )
            best: Optional[int] = None
            for haystack in haystacks:
                if not haystack:
                    continue
                if haystack == needle:
                    score = 0
                elif haystack.startswith(needle):
                    score = 1
                elif needle in haystack:
                    score = 2
                else:
                    continue
                best = score if best is None else min(best, score)
            if best is not None:
                scored.append((best, position, node))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [node for _, _, node in scored[:limit]]

    def find(self, reference: str, limit: int = 10) -> list[OpcNode]:
        """Resolve a free-form reference to a ranked list of candidate nodes.

        Accepted forms (best match first): exact node id, exact browse/simple
        name, ``A/B/C`` path, then substring search.
        """
        if not reference:
            return []
        ref = reference.strip()

        if ref in self.nodes:
            return [self.nodes[ref]]

        exact = self._named(ref)
        if exact:
            return exact[:limit]

        via_path = self._resolve_path(ref)
        if via_path is not None:
            return [via_path]

        return self.search(ref, limit=limit)

    def resolve(self, reference: str) -> Optional[OpcNode]:
        """Return the single best match for *reference*, or ``None``."""
        matches = self.find(reference, limit=1)
        return matches[0] if matches else None

    def resolve_node_id(self, reference: str) -> str:
        """Return the canonical node id for *reference* (fallback: unchanged)."""
        node = self.resolve(reference)
        return node.node_id if node else reference

    def _resolve_path(self, reference: str) -> Optional[OpcNode]:
        parts = [
            part.strip()
            for part in re.split(r"\s*(?:/|\\|>)\s*", reference)
            if part.strip()
        ]
        if len(parts) < 2:
            return None
        for head in self.find(parts[0], limit=5):
            node = self._descend(head, parts[1:])
            if node is not None:
                return node
        return None

    def _descend(self, node: OpcNode, parts: list[str]) -> Optional[OpcNode]:
        for part in parts:
            key = part.lower()
            match = None
            for child_id in node.children:
                child = self.nodes.get(child_id)
                if child is None:
                    continue
                if child.name.lower() == key or child.browse_name.lower() == key:
                    match = child
                    break
            if match is None:
                return None
            node = match
        return node

    # -------------------------------------------------------------- listings
    def list_nodes(
        self,
        node_class: Optional[str] = None,
        parent: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OpcNode]:
        """List nodes, optionally filtered by class, parent or name substring."""
        parent_id = self.resolve_node_id(parent) if parent else None
        needle = query.strip().lower() if query else None

        result: list[OpcNode] = []
        for node in self.nodes.values():
            if node_class and node.node_class.lower() != node_class.lower():
                continue
            if parent_id is not None and node.parent_id != parent_id:
                continue
            if needle:
                haystack = f"{node.name} {node.browse_name} {node.node_id} {node.s7_mapping}".lower()
                if needle not in haystack:
                    continue
            result.append(node)
        return result[offset : offset + limit]

    def children(self, reference: str) -> list[OpcNode]:
        node = self.resolve(reference)
        if node is None:
            return []
        return [self.nodes[cid] for cid in node.children if cid in self.nodes]

    def describe_tree(self, root: Optional[str] = None, depth: int = 3) -> list[dict[str, Any]]:
        """Return a nested, depth-limited view of the hierarchy."""
        if root:
            heads = self.find(root, limit=5)
        else:
            heads = [self.nodes[nid] for nid in self.roots]

        def walk(node: OpcNode, level: int) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "node_id": node.node_id,
                "browse_name": node.browse_name,
                "class": node.node_class,
            }
            if node.value is not None:
                entry["value"] = node.value
            children = [self.nodes[c] for c in node.children if c in self.nodes]
            if level < depth and children:
                entry["children"] = [walk(child, level + 1) for child in children]
            elif children:
                entry["children_truncated"] = len(children)
            return entry

        return [walk(head, 1) for head in heads]
