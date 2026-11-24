from .base import Source
from .bilety24 import Bilety24Source
from .cinemas import FilmwebSource, HeliosSource, KinoMarzenieSource, KinoTarnowSource
from .sport import MaratonyPolskieSource, NinetyMinutSource
from .ebilet import EbiletSource
from .multikino import MultikinoSource
from .pdf_ai import PdfAiSource
from .embedded_json import EmbeddedJsonSource
from .mec import MecSource
from .facebook import FacebookSource
from .html_ai import HtmlAiSource
from .ics import IcsSource
from .jsonld import JsonLdSource
from .rss import RssSource
from .tribe import TribeSource

REGISTRY: dict[str, type[Source]] = {
    cls.type: cls
    for cls in (
        IcsSource, RssSource, TribeSource, JsonLdSource, HtmlAiSource, FacebookSource, MecSource, EmbeddedJsonSource,
        EbiletSource, Bilety24Source, MultikinoSource, PdfAiSource,
        FilmwebSource, HeliosSource, KinoMarzenieSource, KinoTarnowSource, MaratonyPolskieSource, NinetyMinutSource,
    )
}
