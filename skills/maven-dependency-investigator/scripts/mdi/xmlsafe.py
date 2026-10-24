"""Hardened XML parsing for untrusted Maven POM bytes: expat validates the
full captured byte string with all DTD machinery rejected, then the same
bytes go to ElementTree. XInclude stays inert and nothing is ever read from
disk or network.
"""

from xml.etree import ElementTree as ET
from xml.parsers import expat

from mdi.limits import Limits


MAX_XML_DEPTH = 64


class UnsafeXML(ValueError):
    """Raised for oversized, malformed or DTD-bearing XML input."""


class XMLDepthExceeded(UnsafeXML):
    """Raised before tree construction when the hard element ceiling is exceeded."""


def _reject_doctype(*_args) -> None:
    raise UnsafeXML("DOCTYPE declarations are not allowed")


def _reject_entity(*_args) -> None:
    raise UnsafeXML("entity declarations are not allowed")


def _reject_external_ref(*_args) -> None:
    raise UnsafeXML("external entity references are not allowed")


def _safety_parser() -> expat.XMLParserType:
    checker = expat.ParserCreate()
    depth = 0

    def start_element(*_args):
        nonlocal depth
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise XMLDepthExceeded(f"XML element depth exceeds limit of {MAX_XML_DEPTH}")

    def end_element(*_args):
        nonlocal depth
        depth -= 1

    checker.StartElementHandler = start_element
    checker.EndElementHandler = end_element
    checker.StartDoctypeDeclHandler = _reject_doctype
    checker.EntityDeclHandler = _reject_entity
    checker.ExternalEntityRefHandler = _reject_external_ref
    checker.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    return checker


def parse_xml(data: bytes, limits: Limits = Limits()) -> ET.Element:
    """Parse `data` safely and return the root element.

    Raises UnsafeXML when input is oversized, malformed, exceeds 64 element
    levels (root is level one), or carries DTD machinery. The depth check
    precedes tree construction and cannot be relaxed through byte limits.
    There is no fallback to a permissive parse.
    """
    if not isinstance(data, bytes):
        raise TypeError(f"data must be bytes, got {type(data).__name__}")
    if len(data) > limits.xml_bytes:
        raise UnsafeXML(f"XML of {len(data)} bytes exceeds limit of {limits.xml_bytes}")
    try:
        _safety_parser().Parse(data, True)
    except UnsafeXML:
        raise
    except (expat.ExpatError, LookupError, ValueError) as exc:
        raise UnsafeXML(f"malformed XML: {exc}") from exc
    builder = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        builder.feed(data)
        return builder.close()
    except UnsafeXML:
        raise
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise UnsafeXML(f"malformed XML: {exc}") from exc
