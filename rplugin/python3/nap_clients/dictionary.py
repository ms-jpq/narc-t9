from asyncio import Queue
from dataclasses import asdict, dataclass
from os import remove
from os.path import exists, getmtime, join
from typing import Any, AsyncIterator, Dict, Iterator, Sequence, cast

from pynvim import Nvim

from .pkgs.consts import __artifacts__
from .pkgs.da import dump_json, load_json, slurp
from .pkgs.nvim import print
from .pkgs.sql import AConnection
from .pkgs.types import Completion, Context, Seed, Source

__info__ = join(__artifacts__, "dictionary_info.json")
__db__ = join(__artifacts__, "dictionary.db")
__db_ver__ = [0, 1]


@dataclass(frozen=True)
class DictionarySpec:
    path: str
    sep: str


@dataclass(frozen=True)
class Config:
    min_match: int
    sources: Sequence[DictionarySpec]


@dataclass(frozen=True)
class DBConfig:
    version: Sequence[int]
    source_age: Dict[str, int]


def read_config(config: Dict[str, Any]) -> Config:
    min_match = config["min_match"]
    sources = tuple(DictionarySpec(**src) for src in config["sources"])
    return Config(min_match=min_match, sources=sources)


def db_ver(config: Config) -> bool:
    db_exists = exists(__db__)
    source_age = {
        source.path: round(getmtime(source.path))
        for source in config.sources
        if exists(source.path)
    }
    conf = DBConfig(version=__db_ver__, source_age=source_age)
    json = load_json(__info__)
    dump_json(__info__, asdict(conf))
    if not db_exists:
        return True
    elif type(json) is dict:
        disk_conf = DBConfig(**cast(Dict[str, Any], json))
        if disk_conf != conf:
            if db_exists:
                remove(__db__)
            return True
    return False


def read_sources(sources: Sequence[DictionarySpec]) -> Iterator[str]:
    for spec in sources:
        if exists(spec.path):
            data = slurp(spec.path)
            for word in data.split(spec.sep):
                yield word


_INIT = """
CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts4(
  word TEXT NOT NULL UNIQUE,
  nword TEXT NOT NULL
)
"""


_POPULATE = """
INSERT OR IGNORE INTO words(word, nword) VALUES (?, lower(?))
"""


_QUERY = """
SELECT word
FROM words
WHERE
    nword MATCH lower(?)
    AND word <> ?
"""


async def init(conn: AConnection) -> None:
    async with await conn.execute(_INIT):
        pass


async def populate(conn: AConnection, words: Iterator[str]) -> None:
    params = tuple((word, word) for word in words)
    async with await conn.execute_many(_POPULATE, params):
        pass
    await conn.commit()


async def query(conn: AConnection, cword: str, min_matches: int) -> AsyncIterator[str]:
    if len(cword) >= min_matches:
        query = f"{cword}*"
        async with await conn.execute(_QUERY, (query, cword)) as cursor:
            async for row in cursor:
                yield row[0]


def parse_cword(word: str) -> str:
    def cont() -> Iterator[str]:
        for c in reversed(word):
            if c.isalnum():
                yield c
            else:
                break

    cword = "".join(cont())[::-1]
    return cword


async def main(nvim: Nvim, chan: Queue, seed: Seed) -> Source:
    config = read_config(seed.config)
    min_matches = config.min_match
    requires_init = db_ver(config)
    conn = AConnection(__db__)
    if requires_init:
        await print(nvim, "⏳..⌛️")
        await init(conn)
        await populate(conn, words=read_sources(config.sources))
        await print(nvim, "✅")

    async def source(context: Context) -> AsyncIterator[Completion]:
        cword = parse_cword(context.alnums_before)
        async for word in query(conn, cword=cword, min_matches=min_matches):
            yield Completion(
                position=context.position,
                old_prefix=cword,
                new_prefix=word,
                old_suffix="",
                new_suffix="",
            )

    return source
