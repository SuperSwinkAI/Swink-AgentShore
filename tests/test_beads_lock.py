"""Tests for ``_ReadersWriterLock``, the writer-preferring reader/writer lock (#5).

Exercised directly against the lock (not through ``bd()``) so timing is
controlled precisely with events rather than relying on subprocess mocking.
See ``bd110-findings.md`` for the empirical verification that a live bd 1.1.0
embedded store tolerates concurrent ``bd list`` reads, which is the premise
this lock relies on.
"""

from __future__ import annotations

import asyncio

import pytest

from agentshore.beads import _ReadersWriterLock


@pytest.mark.asyncio
async def test_multiple_readers_run_concurrently() -> None:
    lock = _ReadersWriterLock()
    active = 0
    max_active = 0

    async def _reader() -> None:
        nonlocal active, max_active
        async with lock.read():
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(_reader(), _reader(), _reader())
    assert max_active == 3, "all three readers should have been active at once"


@pytest.mark.asyncio
async def test_writer_waits_for_active_readers_then_excludes_them() -> None:
    lock = _ReadersWriterLock()
    events: list[str] = []
    reader_active = asyncio.Event()

    async def _reader() -> None:
        async with lock.read():
            events.append("reader_start")
            reader_active.set()
            await asyncio.sleep(0.03)
            events.append("reader_end")

    async def _writer() -> None:
        await reader_active.wait()
        async with lock.write():
            events.append("writer_start")
            events.append("writer_end")

    await asyncio.gather(_reader(), _writer())
    assert events == ["reader_start", "reader_end", "writer_start", "writer_end"], events


@pytest.mark.asyncio
async def test_writers_are_mutually_exclusive() -> None:
    lock = _ReadersWriterLock()
    timeline: list[str] = []

    async def _writer(tag: str) -> None:
        async with lock.write():
            timeline.append(f"{tag}_start")
            await asyncio.sleep(0.02)
            timeline.append(f"{tag}_end")

    await asyncio.gather(_writer("a"), _writer("b"))
    # Whichever writer goes first must fully finish before the other starts —
    # no interleaving of the two writers' start/end pairs.
    assert timeline[0].endswith("_start")
    assert timeline[1].endswith("_end")
    assert timeline[0].split("_")[0] == timeline[1].split("_")[0], timeline
    assert timeline[2].endswith("_start")
    assert timeline[3].endswith("_end")


@pytest.mark.asyncio
async def test_pending_writer_blocks_a_reader_that_arrives_after_it() -> None:
    """Writer-preference: a reader arriving while a writer is waiting queues behind it.

    Sequence: reader1 acquires the read lock; while reader1 is still active,
    a writer starts waiting; while the writer is waiting, reader2 arrives.
    Reader2 must not cut in front of the writer once reader1 releases —
    otherwise a steady stream of reads could starve the writer indefinitely.
    """
    lock = _ReadersWriterLock()
    events: list[str] = []
    reader1_active = asyncio.Event()
    writer_waiting = asyncio.Event()

    async def _reader1() -> None:
        async with lock.read():
            events.append("reader1_start")
            reader1_active.set()
            await asyncio.sleep(0.03)
            events.append("reader1_end")

    async def _writer() -> None:
        await reader1_active.wait()
        write_task = asyncio.ensure_future(_do_write())
        await asyncio.sleep(0.005)  # let the writer register itself as waiting
        writer_waiting.set()
        await write_task

    async def _do_write() -> None:
        async with lock.write():
            events.append("writer_start")
            await asyncio.sleep(0.01)
            events.append("writer_end")

    async def _reader2() -> None:
        await writer_waiting.wait()
        async with lock.read():
            events.append("reader2_start")
            events.append("reader2_end")

    await asyncio.gather(_reader1(), _writer(), _reader2())

    assert events == [
        "reader1_start",
        "reader1_end",
        "writer_start",
        "writer_end",
        "reader2_start",
        "reader2_end",
    ], events
