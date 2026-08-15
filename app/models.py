"""Shared data models: text blocks, pages and documents."""

from __future__ import annotations

from dataclasses import dataclass, field

BBox = tuple[float, float, float, float]


@dataclass(slots=True)
class Block:
    """A text block extracted from a page; the unit of translation."""

    id: str
    text: str
    bbox: BBox
    font_size: float
    font_name: str
    is_bold: bool = False
    is_italic: bool = False
    is_translatable: bool = True
    line_count: int = 1
    color: int = 0  # packed sRGB, as returned by PyMuPDF span["color"] (0 = black)


@dataclass(slots=True)
class PageData:
    """A single page: dimensions, text blocks and scanned flag."""

    number: int  # 0-based
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)
    is_scanned: bool = False


@dataclass(slots=True)
class DocumentData:
    """A whole document: metadata and pages."""

    path: str
    page_count: int
    title: str | None = None
    author: str | None = None
    pages: list[PageData] = field(default_factory=list)

    def all_blocks(self) -> list[Block]:
        """Return every block of every page."""
        return [block for page in self.pages for block in page.blocks]

    def translatable_blocks(self) -> list[Block]:
        """Return only the blocks worth sending to a translator."""
        return [block for block in self.all_blocks() if block.is_translatable]
